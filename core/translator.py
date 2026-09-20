# -*- coding: utf-8 -*-
"""OpenAI 兼容协议的 LLM 翻译引擎（DeepSeek / OpenAI / 本地 Ollama、LM Studio 等）。"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import texttags

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# 上下文/输入超限类错误的特征词（本地模型窗口写满时最常见）
_CONTEXT_ERR_RE = re.compile(
    r"context size|context length|context window|too long|maximum context|"
    r"exceed\w*\s+\w*\s*(context|token|length)|(token|length)\s+(limit|exceed)",
    re.I)

# 连续多少个批次整批失败就中止本轮（本地模型/服务端坏掉时，避免无意义地刷几小时）
_FAIL_STREAK_LIMIT = 20

# Ren'Py 成对文本标签（开/闭必须配对）；其余（w/p/nw/image/fast...）为单标记
_PAIRED_TAGS = {"i", "b", "s", "u", "color", "size", "font", "cps", "alpha",
                "outline", "spacing", "a", "rb", "rt", "art", "noalt"}
_TAG_RE = re.compile(r"\{\s*(/?)\s*([a-zA-Z][a-zA-Z0-9_]*)\s*(?:=[^}]*)?\}")


def fix_rpy_tags(text):
    """修复模型输出中错乱的 Ren'Py 文本标签：
    0. `{image:x}` 这类用冒号传参的写法改成 `{image=x}`——引擎只认 `=`，冒号写法能
       骗过引擎自带的 lint、却会在渲染时抛 Unknown text tag（见 core/texttags.py）；
    1. `{ i }`/`{ /i }` 这类带空格的写成标准 `{i}`/`{/i}`；
    2. 开标签被误写成闭标签（如 `{/i}强调{/i}`）时翻转成 `{/i}` 前的 `{i}`；
    3. 嵌套交叉时先补齐内层未闭合标签。
    行尾不补闭合：Ren'Py 对话行末自动闭合（原文也常省略 `{/i}`）。
    """
    if not text or "{" not in text:
        return text
    text = texttags.fix(text)
    text = re.sub(r"\{\s*/\s*([a-zA-Z][a-zA-Z0-9_]*)\s*\}", r"{/\1}", text)
    text = re.sub(r"\{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*(=[^}]*)?\s*\}", r"{\1\2}", text)

    out, stack, pos = [], [], 0
    for m in _TAG_RE.finditer(text):
        out.append(text[pos:m.start()])
        closing, name = m.group(1) == "/", m.group(2).lower()
        seg = m.group(0)
        if name not in _PAIRED_TAGS:
            out.append(seg)
        elif closing:
            if name in stack:
                while stack and stack[-1] != name:
                    out.append("{/%s}" % stack.pop())
                stack.pop()
                out.append(seg)
            else:
                out.append("{%s}" % name)
                stack.append(name)
        else:
            stack.append(name)
            out.append(seg)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def chat_raw(cfg, messages, timeout=None):
    """发一次对话请求，返回 (content, finish_reason)。

    finish_reason == "length" 表示回复被输出上限截断：调用方应保留已解析出的条目，
    把剩下的拆小重试，而不是当成整批失败重来。
    """
    payload = {"model": cfg["model"], "messages": messages,
               "temperature": cfg.get("temperature", 0.6)}
    # 输出上限：0 = 不发送（云端 API 保持原样，也避开只认 max_completion_tokens 的新模型）。
    # 本地模型务必设置——不设时模型可以一直写到上下文窗口用满，llama.cpp/LM Studio 会直接抛
    # 500 "Context size has been exceeded" 并连带杀死同批并发，整轮翻译因此卡死。
    try:
        max_tokens = int(cfg.get("max_tokens", 0) or 0)
    except (TypeError, ValueError):
        max_tokens = 0
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    # Qwen 官方建议：量化版本地模型加 presence_penalty≈1.5 抑制复读（长篇批量翻译易触发）
    if float(cfg.get("presence_penalty", 0) or 0):
        payload["presence_penalty"] = float(cfg["presence_penalty"])
    # 思考模式开关（DeepSeek 官方 OpenAI 格式：thinking + reasoning_effort）。
    # DeepSeek V4 系列默认开启思考（effort=high），思考内容按输出 token 计费；
    # 翻译这类结构化任务建议关闭。auto = 不发送参数，兼容不认识该字段的服务商。
    thinking = str(cfg.get("thinking") or "auto").lower()
    if thinking == "off":
        payload["thinking"] = {"type": "disabled"}
    elif thinking in ("low", "high"):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = thinking
    resp = requests.post(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer %s" % cfg.get("api_key", ""), "Content-Type": "application/json"},
        json=payload,
        timeout=timeout or cfg.get("timeout", 240),
    )
    resp.raise_for_status()
    j = resp.json()
    choice = (j.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if not (content or "").strip():
        # 典型签名：HTTP 200、finish_reason=stop 但 completion_tokens=0。
        # Gemini 系端点触发安全过滤时就这样静默返回空候选（blockReason 被代理吞掉），
        # 若不在此报错，下游只会看到"找不到 JSON"，掩盖真实原因。
        raise ValueError("模型返回了空回复（内容很可能触发了上游安全过滤，可换模型重试）")
    return content, choice.get("finish_reason")


def chat(cfg, messages, timeout=None):
    """发一次对话请求，返回回复文本（不需要 finish_reason 的调用方用这个）。"""
    return chat_raw(cfg, messages, timeout)[0]


def test_engine(cfg):
    text = chat(cfg, [{"role": "user", "content": "回复“OK”两个字母即可。"}], timeout=30)
    return text.strip()[:40]


def list_models(cfg):
    """GET /models 拉取可用模型列表（OpenAI 兼容端点：DeepSeek/Ollama/LM Studio 等均支持）。"""
    base = cfg["base_url"].rstrip("/")
    headers = {"Authorization": "Bearer %s" % cfg.get("api_key", "")}
    timeout = cfg.get("timeout", 20) or 20
    try:
        resp = requests.get(base + "/models", headers=headers, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        alt = base[:-3] if base.endswith("/v1") else None
        if not alt:
            raise
        resp = requests.get(alt + "/models", headers=headers, timeout=timeout)
        resp.raise_for_status()
    j = resp.json()
    arr = j.get("data") if isinstance(j, dict) else j
    ids = sorted(str(m.get("id")) for m in arr or [] if isinstance(m, dict) and m.get("id"))
    if not ids:
        raise ValueError("接口没有返回任何模型")
    return ids


def verify_model(cfg, model_id):
    """只校验单个模型（不拉全表）：优先 GET /models/{id}（OpenRouter/OpenAI 支持，附元信息）；
    端点不支持时退化为发一条极小的对话请求实测。返回 dict，失败抛异常。"""
    from urllib.parse import quote
    base = cfg["base_url"].rstrip("/")
    headers = {"Authorization": "Bearer %s" % cfg.get("api_key", "")}
    try:
        resp = requests.get("%s/models/%s" % (base, quote(model_id, safe=":/.-_")),
                            headers=headers, timeout=cfg.get("timeout", 20) or 20)
        if resp.status_code == 200:
            j = resp.json()
            info = j.get("data", j) if isinstance(j, dict) else {}
            return {"via": "models", "info": info if isinstance(info, dict) else {}}
    except Exception:
        pass
    try:
        text = chat(dict(cfg, model=model_id),
                    [{"role": "user", "content": "回复“OK”两个字母即可。"}], timeout=30)
    except requests.HTTPError as e:
        try:
            detail = (e.response.json().get("error") or {}).get("message") or e.response.text[:200]
        except Exception:
            detail = str(e)
        raise RuntimeError("模型不可用：%s" % detail)
    return {"via": "chat", "reply": (text or "").strip()[:40]}


def _salvage_objects(text):
    """从残缺文本里逐个抢救完整的 {...} 对象（回复被输出上限截断时用）。
    逐字符扫描并跟踪字符串/转义状态，因此译文中带 {w}{b} 这类标签也不会误判括号。"""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, instr, esc, j = 0, False, False, i
        while j < n:
            c = text[j]
            if instr:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:j + 1])
                        if isinstance(obj, dict):
                            out.append(obj)
                    except Exception:
                        pass
                    break
            j += 1
        i = j + 1 if j > i else i + 1
    return out


def _parse_json_array(text):
    text = _FENCE.sub("", text.strip())
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            arr = json.loads(text[start:end + 1])
            if isinstance(arr, list):
                return arr
        except Exception:
            pass
    # 截断或语法损坏：抢救而已完整的条目（宁可译一部分，也不要整批重来）
    out = _salvage_objects(text[start if start >= 0 else 0:])
    if not out:
        raise ValueError("回复中找不到 JSON 数组")
    return out


def _is_context_error(exc):
    """判断异常是否为"输入/上下文超出模型窗口"。

    这类错误原样重试必然再失败（同样的输入、同样的窗口），正确做法是把批次拆小重试；
    与网络抖动、服务端 5xx 等"等一下可能就好"的错误区分开。
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = ""
        try:
            body = resp.text or ""
        except Exception:
            pass
        if resp.status_code in (400, 413, 422):
            return True
        if resp.status_code >= 500 and _CONTEXT_ERR_RE.search(body):
            return True
    return bool(_CONTEXT_ERR_RE.search(str(exc)))


def est_tokens(text):
    """粗估 token 数：CJK 字符按 1 token/字，其余按 1 token/3.2 字符（偏保守，宁可多估）。"""
    cjk = 0
    for ch in text:
        if "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af":
            cjk += 1
    return int(cjk + (len(text) - cjk) / 3.2)


def _align_results(arr, batch_keys):
    """把模型回复对齐回批次 key：优先精确匹配 i；模型改写 key 时按顺序对齐兜底。
    兼容：{"i","t"} / {"i","text"} / {"i","translation"} / 纯字符串数组。

    若回复里出现重复的 i（本地模型复读时的典型症状），则不再做顺序兜底——
    否则一整串重复条目会被按顺序贴到别的行上，造成"译文串行"这种静默错误。
    """
    keyset = set(batch_keys)
    got = {}
    extras = []
    seen_ids = set()
    dup_id = False
    for it in arr:
        if isinstance(it, str):
            t = fix_rpy_tags(it.strip().strip('"').strip())
            if t:
                extras.append(t)
            continue
        if not isinstance(it, dict):
            continue
        t = it.get("t") or it.get("text") or it.get("translation")
        if t is None or not str(t).strip():
            continue
        t = fix_rpy_tags(str(t).strip().strip('"').strip())
        i = it.get("i")
        if isinstance(i, str) and i in keyset:
            if i in got or i in seen_ids:
                dup_id = True
            else:
                got[i] = t
                seen_ids.add(i)
            continue
        extras.append(t)
    if dup_id:
        extras = []
    unmatched = [k for k in batch_keys if k not in got]
    # 顺序兜底仅在"模型完全没回传 i"且数量恰好的字符串数组场景启用：
    # 部分 i 匹配 + 顺序兜底会把漏译条目之后的所有译文整体错位一位（串行错位），
    # 宁可让缺失的条目走重试，也不能错位。
    if not got and unmatched and len(extras) == len(unmatched):
        for k, t in zip(unmatched, extras):
            got[k] = t
    return got


def _glossary_for(texts, glossary):
    blob = "\n".join(texts).lower()
    hits = []
    for g in glossary:
        if g.get("src") and g["src"].lower() in blob:
            hits.append("%s => %s" % (g["src"], g["dst"]))
    return hits


def _relations_for(whos, relations):
    if not relations or not whos:
        return []
    blob = set(w.lower() for w in whos if w)
    out = []
    for r in relations:
        tag = (r.get("speaker") or "").lower()
        if tag and (tag in blob or any(tag in w.lower() for w in blob)):
            name = r.get("name") or r.get("speaker")
            if r.get("name_cn"):
                name = "%s(%s)" % (name, r["name_cn"])
            out.append("%s(%s): %s" % (name, r.get("gender", "?"), r.get("relation", "")))
    return out


def names_line_for(relations, limit=40):
    """从人物关系表生成人名对照行（每个请求都注入，保证全文译名统一）。"""
    pairs = []
    for r in relations or []:
        en = (r.get("name") or "").strip()
        cn = (r.get("name_cn") or "").strip()
        if en and cn and en.lower() != cn.lower():
            pairs.append("%s=%s" % (en, cn))
        if len(pairs) >= limit:
            break
    return "；".join(pairs)


DEFAULT_SYSTEM_PROMPT = """你是视觉小说游戏的专业翻译。把给出的游戏文本从英文翻译成{lang}。
要求：
1. 保留 Ren'Py 文本标签与占位符，如 {w}{b}{/b}{i}{cps=20}、%% 等，原样保留不翻译。
   方括号里的变量名必须逐字符照抄原文（大小写、下划线都与原文完全一致，不许"规范化"，
   原文没有的变量一个都不许加）。
2. 语气自然，符合人物身份与场景；口语化，不要翻译腔。
3. 译文中不要使用英文双引号，可用中文引号。
4. 条目中的 ctx 字段是上下文参考（前文/后文），只用于理解语境，不要翻译、不要输出。
5. 只输出 JSON 数组：[{"i": 与输入完全相同的条目i, "t": "译文"}, ...]，i 必须原样回传，不要输出其他内容。"""


def build_system_prompt(target_lang, glossary_hits, relation_hits, names_line="", custom=None):
    """custom 为用户自定义的 System Prompt（设置页可编辑）；{lang} 占位符会被替换成目标语言。
    人名对照/词汇表/人物关系始终自动附加在提示词之后，不受自定义影响。"""
    base = (custom or "").strip() or DEFAULT_SYSTEM_PROMPT
    lines = [base.replace("{lang}", target_lang)]
    # 变量照抄说在自定义提示词之后，用户的提示词改了也照样生效。**不举具体变量名**：
    # 提示词里出现过 [player_name] 之后，模型把原文的 [mc_name] 也改写成了 [player_name]
    # （2026-09-17 The Home of Pleasure 的事故）——这类"反面例子"同样会被抄走。
    lines.append("变量照抄（硬性要求）：原文方括号里的变量要逐字符原样保留——大小写、下划线都与原文"
                 "完全一致，不许“规范化”，也不许换成别处见过的变量名；原文里没有的变量一个都不许加。")
    if names_line:
        lines.append("人名对照（文中出现的这些人名必须统一使用对应译名，禁止自行音译出其他写法）：" + names_line)
    if glossary_hits:
        lines.append("词汇表（必须严格遵守其中的译名）：" + "；".join(glossary_hits))
    if relation_hits:
        lines.append("人物关系（据此确定称谓，如姐姐/妹妹/哥哥/弟弟）：" + "；".join(relation_hits))
    if "JSON" not in base:  # 用户改写后丢了输出格式约定时兜底，保证回复可解析
        lines.append('输出格式（必须遵守）：只输出 JSON 数组：[{"i": 与输入完全相同的条目i, "t": "译文"}, ...]，i 必须原样回传。')
    return "\n".join(lines)


def translate_jobs(jobs, engine, glossary, relations, target_lang="简体中文",
                   progress=None, should_stop=None, log=print, retranslate_keys=None,
                   seed=None, commit=None):
    """翻译 jobs（build_jobs 的输出），返回本任务内存中的 translations 字典。

    seed：断点续翻与同文去重的种子译文（调用方从项目库取，通常是全部当前译文
    去掉已请求重译的出现位置）；不再从派生镜像文件读取。
    commit：按记录事务提交的入口（映射 {任务 key: 译文}），每个批次的结果在
    解析、标签修复、变量还原之后立即提交一次（项目库一次一小事务）——进程中断
    时已提交批次完整一致，未提交批次不产生半写状态，不再有"整份覆盖派生镜像"
    的最后写入者覆盖窗口。commit 为 None 时只保留内存结果（轻量调用方用）。

    retranslate_keys：要求重新翻译的任务 key（用户对具体译文发起的重译请求）。
    这些 key 不吃同文去重缓存——即使同原文的其它 key 已有译文也要单独请求，
    请求结果由调用方决定去向（人工确认的译文只进候选）。"""
    translations = dict(seed or {})
    lock = threading.Lock()
    retrans = set(retranslate_keys or ())

    def commit_results(fixed):
        """把一批已修复的模型结果交给提交入口（调用方负责事务与冲突处理）。

        按记录事务提交：每批立即落库（项目库一次一小事务），进程中断时已提交
        批次保持一致、未提交批次不产生半写状态。旧实现每 20 条/15 秒整份重写
        派生镜像文件——写盘时持锁把并发压成事实上的 1，且最后一次整份写入与
        内存状态之间没有一致边界。"""
        if fixed and commit is not None:
            commit(fixed)

    # 相同文本去重：一次翻译，多处复用（同句同译，天然保证一致性）
    # 断点续翻：同一文本的任意 key 已有译文即视为已译（即使上次中途退出没来得及展开）
    text_map = {}
    for j in jobs:
        if j["old"]:
            text_map.setdefault(j["old"], []).append(j["key"])
    dedup = {}
    reused = {}
    for text, keys in text_map.items():
        done = None
        for k in keys:
            if k in retrans:
                continue  # 请求重译的 key 不复用同文缓存，稍后单独请求
            v = translations.get(k)
            if not v or v.count("{}") != text.count("{}"):
                continue
            # [变量] 与原文对不上的译文（大小写被改、名字被换、变量被整个翻译掉）
            # 与无译文等效——回填时同样会被 _expand_translations 拒用，不能当作
            # "已译"跳过，否则坏缓存永远得不到重译
            v2, bad_interp = texttags.fix_interps(text, v)
            if bad_interp:
                continue
            done = v2
            break
        if done:
            for k in keys:
                if k not in retrans and k not in translations:
                    translations[k] = done
                    reused[k] = done
        else:
            dedup[keys[0]] = text
        for k in keys:
            if k in retrans:
                dedup[k] = text
    # 同文复用的种子展开也是一条模型结果：按记录事务提交（k 在 seed 里已有
    # 译文的自然跳过），进程中断后断点续翻才不会把这些 key 当作未译重发请求
    commit_results(reused)

    todo_keys = list(dedup.keys())
    who_ctx = {j["key"]: (j.get("who", ""), j.get("ctx") or ([], [])) for j in jobs}
    total_all = len(text_map)
    done_all = max(0, total_all - len(todo_keys))
    if progress and total_all:
        progress(done_all, total_all)

    bs = max(1, engine.get("batch_size", 12))
    batches = [todo_keys[i:i + bs] for i in range(0, len(todo_keys), bs)]
    max_retry = max(0, int(engine.get("max_retry", 4)))
    try:
        prompt_budget = int(engine.get("max_prompt_tokens", 0) or 0)
    except (TypeError, ValueError):
        prompt_budget = 0

    fail_streak = [0]   # 连续整批失败的次数（熔断用）
    interp_bad = [0]    # 译文里出现原文没有的 [变量] 的条数（回填时会被拒用）
    aborted = [None]    # 熔断原因；置位后所有工作线程尽快退出

    def split_and_retry(batch, attempt, depth, why):
        """把批次一分为二重试（输入超预算 / 超出模型上下文 / 回复被截断时用）。
        depth 限制拆分层数：服务端整体不可用时，不该把每个批次都碎成单条白刷请求。"""
        if len(batch) < 2 or depth >= 3:
            return False
        half = len(batch) // 2
        log("%s，拆成 %d + %d 条分别重试" % (why, half, len(batch) - half))
        do_batch(batch[:half], attempt, depth + 1)
        do_batch(batch[half:], attempt, depth + 1)
        return True

    def do_batch(batch, attempt=0, depth=0):
        nonlocal done_all
        if aborted[0] or (should_stop and should_stop()):
            return
        items = []
        for key in batch:
            text = dedup[key]
            who, (pre, nxt) = who_ctx.get(key, ("", ([], [])))
            it = {"i": key, "text": text}
            if who:
                it["who"] = who
            if pre or nxt:
                it["ctx"] = "前文: %s | 后文: %s" % (" / ".join(pre[-2:]), " / ".join(nxt[:2]))
            items.append(it)
        sys_prompt = build_system_prompt(
            target_lang,
            _glossary_for([dedup[k] for k in batch], glossary),
            _relations_for([who_ctx.get(k, ("",))[0] for k in batch], relations),
            names_line_for(relations),
            engine.get("system_prompt"))
        user_content = json.dumps(items, ensure_ascii=False)
        # 输入预算守卫：超预算先拆小（本地小窗口模型靠这个避免直接撞上上下文上限）
        if prompt_budget > 0 and len(batch) > 1:
            est = est_tokens(sys_prompt) + est_tokens(user_content)
            if est > prompt_budget:
                if split_and_retry(batch, attempt, depth,
                                   "输入约 %d token，超过预算 %d" % (est, prompt_budget)):
                    return
        try:
            reply, finish = chat_raw(engine, [{"role": "system", "content": sys_prompt},
                                              {"role": "user", "content": user_content}])
            got = _align_results(_parse_json_array(reply), batch)
            # 入库前先按原文还原 [变量]（模型偶尔会把 [PlayerName] 改名成
            # [player_name] 或整个翻译掉）；还原不了的照存，回填时会被拒用。
            # 先提交（记录事务）、再并入内存：提交失败走批次重试，不会出现在
            # "内存已记、库里没有"的半提交状态。
            fixed = {}
            for k, t in got.items():
                t, bad_interp = texttags.fix_interps(dedup.get(k, ""), t)
                if bad_interp:
                    interp_bad[0] += 1
                    if interp_bad[0] <= 10:
                        log("⚠ 译文出现原文没有的 [变量] %s（%s）——回填时会拒用这条"
                            % (bad_interp, k))
                fixed[k] = t
            commit_results(fixed)
            with lock:
                translations.update(fixed)
                done_all += len(fixed)
                if fixed:
                    fail_streak[0] = 0
            if progress:
                progress(min(done_all, total_all), total_all)
            missing = [k for k in batch if k not in got]
            if not missing:
                return
            if finish == "length" and split_and_retry(missing, attempt, depth, "回复被输出上限截断"):
                return
            if attempt < max_retry:
                time.sleep(2 ** attempt)
                do_batch(missing, attempt + 1, depth)
            else:
                log("批次有 %d 行多次翻译失败，跳过" % len(missing))
        except Exception as e:
            if aborted[0]:
                return
            if _is_context_error(e) and split_and_retry(
                    batch, attempt, depth, "超出模型上下文（%s）" % str(e)[:120].replace("\n", " ")):
                return
            if attempt < max_retry:
                time.sleep(min(30, 2 ** attempt))
                do_batch(batch, attempt + 1, depth)
            else:
                fail_streak[0] += 1
                log("批次失败放弃: %s" % e)
                if fail_streak[0] >= _FAIL_STREAK_LIMIT:
                    aborted[0] = (
                        "连续 %d 个批次整批失败，已中止本轮翻译（避免继续空转）。\n"
                        "最后一个错误：%s\n"
                        "本地模型：检查推理服务（LM Studio/Ollama）的上下文长度与并行请求数，"
                        "并在设置页填好「单次回复上限 / 输入预算」；\n"
                        "在线 API：检查接口地址、API Key 与额度。" % (fail_streak[0], e))
                    raise RuntimeError(aborted[0])

    workers = max(1, int(engine.get("concurrency", 8)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(do_batch, b) for b in batches]
            for f in as_completed(futures):
                f.result()
    finally:
        # 去重展开：同一文本里还没有译文的 key 复用已有译文。
        # 已有自己译文的 key 绝不覆盖——否则「同一句英文在不同分支的既有译法」和
        # 被人工排除补丁（ipatch_skip.json）的条目会被一起抹平成同一条译文。
        # 请求重译的 key 不参与复用：要么拿到自己的新结果，要么留给下一轮重试。
        # 放在 finally 里，熔断中止时也要把已完成的部分提交（断点续翻靠它）；
        # 展开结果同样走记录事务提交（经人工译文保护入口，冲突只进候选）。
        expanded = {}
        for text, keys in text_map.items():
            t = next((translations[k] for k in keys if translations.get(k)), None)
            if not t:
                continue
            for k in keys:
                if k not in retrans and not translations.get(k):
                    translations[k] = t
                    expanded[k] = t
        commit_results(expanded)
    if interp_bad[0]:
        log("⚠ 本次有 %d 条译文的 [变量] 与原文对不上，回填时会被拒用（这些行先显示英文）；"
            "多为模型把变量改名，重跑一次通常就好" % interp_bad[0])
    return translations
