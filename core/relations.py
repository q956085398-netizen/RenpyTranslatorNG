# -*- coding: utf-8 -*-
"""人物关系：本地抽样统计 + 在线/自定义模型推断关系草表。"""
import json
import re
import time

from .translator import chat, test_engine

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _extract_json(reply):
    """从模型回复中提取 JSON 对象（容忍 ``` 围栏和前后多余文字）；失败抛异常。
    返回 dict；模型漏掉外层对象直接给了数组时，兜底包装成 {"characters": [...]}。"""
    text = _FENCE.sub("", (reply or "").strip())
    b0, a0 = text.find("{"), text.find("[")
    if a0 >= 0 and (b0 < 0 or a0 < b0):     # 回复是（或以）数组开头：按数组切片
        start, end = a0, text.rfind("]")
    else:
        start, end = b0, text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复中找不到 JSON 对象")
    data = json.loads(text[start:end + 1])
    if isinstance(data, list):
        data = {"characters": data}
    if not isinstance(data, dict):
        raise ValueError("回复不是 JSON 对象")
    return data


def speaker_stats(dump, top=None):
    """本地统计每个说话人的台词数与样本，不消耗 token。top=None 表示全部说话人。"""
    per = {}
    for e in dump.values():
        for node in e.get("nodes", []):
            if node["type"] == "say" and node.get("what"):
                who = node.get("who") or ""
                per.setdefault(who, []).append(node["what"])
    stats = sorted(per.items(), key=lambda kv: -len(kv[1]))
    if top:
        stats = stats[:top]
    return stats


def _sample_len(whats, per_char):
    if len(whats) > per_char:
        step = len(whats) // per_char
        whats = whats[::step][:per_char]
    return sum(min(len(w), 150) + 12 for w in whats)


def _chunk_stats(stats, per_char, budget=24000):
    """把说话人按样本字符量分批，保证单次请求不超模型上下文。返回若干批 stats。"""
    chunks, cur, size = [], [], 0
    for st in stats:
        est = _sample_len(st[1], per_char)
        if cur and size + est > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(st)
        size += est
    if cur:
        chunks.append(cur)
    return chunks


def sample_for_prompt(stats, per_char=40, known_names=None):
    parts = []
    for who, whats in stats:
        if len(whats) > per_char:
            step = len(whats) // per_char
            whats = whats[::step][:per_char]
        joined = "\n".join("  - " + w.replace("\n", " ")[:150] for w in whats)
        real = (known_names or {}).get(who)
        if not real and who.startswith('"') and who.endswith('"'):
            real = who[1:-1]
        head = "【说话人代号 %s】" % (who or "(旁白)")
        if real and real.lower() != who.lower():
            head += "（真实人名：%s）" % real
        head += "（共%d句，抽样如下）" % len(whats)
        parts.append(head + "\n" + joined)
    return "\n\n".join(parts)


PROMPT = """以下是某英文视觉小说中各说话人（角色代号）的抽样台词。请根据台词内容、语气、相互称呼，推断人物信息与人物之间的关系（注意 sister/brother/mother 等词需要明确出长幼与性别）。
标注了「真实人名」的说话人：name 字段必须原样使用该真实人名（这是从游戏脚本解析出的准确人名，禁止改成别的写法），name_cn 是它的中译名（音译或意译，全篇必须唯一、前后一致）。
没有标注真实人名的说话人：根据台词推测人名填入 name。
只输出 JSON，格式：
{{"characters":[{{"speaker":"角色代号","name":"人名(有真实人名时必须原样使用)","name_cn":"统一的中文名","gender":"男/女/未知","relation":"与主角的关系及长幼性别,如 主角的姐姐/主角的弟弟/房东太太/未知","note":"其他有助于翻译一致性的备注(说话风格、称呼习惯等)"}}]}}

{samples}"""


def _attempt_batch(batch_cfg, messages, max_retry, ci, n_batches, log,
                   should_stop=None, tag=""):
    """单批扫描（含重试）。返回 (data, 最后回复, 最后错误)；全部失败时 data 为 None。"""
    data, reply, last_err = None, "", ""
    for attempt in range(max_retry):
        try:
            reply = chat(batch_cfg, messages)
            data = _extract_json(reply)
            break
        except Exception as e:
            last_err = str(e)
            data = None
            if attempt < max_retry - 1:
                wait = min(30, 2 ** attempt)
                log("第 %d/%d 批失败%s（%s），%d 秒后重试…"
                    % (ci + 1, n_batches, tag, last_err[:120], wait))
                if should_stop and should_stop():
                    break
                time.sleep(wait)
    return data, reply, last_err


def draft_relations(dump, scan_cfg, engine, log=print, known_names=None,
                    progress=None, should_stop=None):
    """调用模型生成关系草表（覆盖全部说话人，样本过大时自动分批）。
    scan_cfg.use == 'custom' 时用独立端点。
    known_names: {变量代号: 真实人名}（从游戏 define 解析），可大幅提高准确率。"""
    cfg = dict(engine)
    if scan_cfg.get("use") == "custom":
        cfg.update({"base_url": scan_cfg["base_url"], "api_key": scan_cfg.get("api_key", ""),
                    "model": scan_cfg.get("model") or engine["model"]})
    stats = speaker_stats(dump)
    if not stats:
        raise RuntimeError("没有提取到任何对白，请先执行提取")
    per_char = scan_cfg.get("sample_per_char", 40)
    chunks = _chunk_stats(stats, per_char)
    if len(chunks) > 1:
        log("说话人 %d 个，样本较大，分 %d 批扫描（工具是通用的，不限制角色数量）"
            % (len(stats), len(chunks)))
    if known_names:
        got = sum(1 for w, _ in stats if w in known_names or (w.startswith('"') and w.endswith('"')))
        log("已注入 %d 个游戏脚本解析出的真实人名（AI 只需译名和关系，不再猜名）" % got)
    log("扫描模型: %s @ %s（说话人 %d 个）" % (cfg["model"], cfg["base_url"], len(stats)))

    # 分析+输出 JSON 用低温度：显著减少坏 JSON（引擎的 0.8 高温是坏输出主因之一）
    scan_temp = float(scan_cfg.get("temperature", 0.3))
    max_retry = max(1, int(cfg.get("max_retry", 4)))
    # 主引擎兜底：扫描用独立端点且与主翻译引擎不同时，某批重试耗尽就换主引擎再试
    # （Gemini 系代理会对部分内容静默返回空回复，换一家引擎通常能过）
    fallback_cfg = None
    if scan_cfg.get("use") == "custom":
        main = dict(engine)
        if (main.get("base_url"), main.get("model")) != (cfg.get("base_url"), cfg.get("model")):
            fallback_cfg = dict(main, temperature=scan_temp)
    chars, failed, last_err = [], 0, ""
    for ci, chunk in enumerate(chunks):
        if should_stop and should_stop():
            break
        messages = [{"role": "system", "content": "你是视觉小说文本分析助手，只输出 JSON。"},
                    {"role": "user", "content": PROMPT.format(
                        samples=sample_for_prompt(chunk, per_char, known_names))}]
        batch_cfg = dict(cfg, temperature=scan_temp)
        data, reply, last_err = _attempt_batch(
            batch_cfg, messages, max_retry, ci, len(chunks), log, should_stop)
        if data is None and fallback_cfg and not (should_stop and should_stop()):
            log("第 %d/%d 批改用主翻译引擎（%s @ %s）重试…"
                % (ci + 1, len(chunks), fallback_cfg["model"], fallback_cfg["base_url"]))
            data, reply, last_err = _attempt_batch(
                fallback_cfg, messages, max_retry, ci, len(chunks), log,
                should_stop, tag="（主引擎）")
        if data is None:
            failed += 1
            head = " ".join((reply or last_err).split())[:200]
            both = "（扫描引擎与主引擎均失败）" if fallback_cfg else ""
            log("第 %d/%d 批重试耗尽，跳过%s（缺的角色可在「③ 人物关系」页手动补，或之后再重扫）。\n"
                "  模型回复开头：%s" % (ci + 1, len(chunks), both, head or "(空回复)"))
            continue
        part = data.get("characters", [])
        for c in part:
            c.setdefault("speaker", "")
            c.setdefault("name", "")
            c.setdefault("name_cn", "")
            c.setdefault("gender", "未知")
            c.setdefault("relation", "未知")
            c.setdefault("note", "")
            # 真实人名优先：AI 改写了 name 时纠正回来
            real = (known_names or {}).get(c["speaker"])
            if real:
                c["name"] = real
        chars.extend(part)
        if len(chunks) > 1:
            log("关系扫描进度：%d/%d 批（累计 %d 个角色）" % (ci + 1, len(chunks), len(chars)))
        if progress:
            progress(ci + 1, len(chunks))
    if not chars and failed and not (should_stop and should_stop()):
        # 一批都没成：抛错让上层保留原 relations.json，不覆盖
        raise RuntimeError("全部 %d 批扫描均失败，原关系表未改动。最后一次错误：%s"
                           % (failed, last_err[:200]))
    if failed:
        log("⚠ %d/%d 批扫描失败被跳过，本次只得到 %d 个角色；缺失角色可手动补，或网络好转后重扫"
            % (failed, len(chunks), len(chars)))
    return chars


def relations_summary(characters, limit=60):
    lines = []
    for c in characters[:limit]:
        if not c.get("speaker"):
            continue
        lines.append("%s=%s(%s), %s" % (c["speaker"], c.get("name", "?"), c.get("gender", "?"), c.get("relation", "")))
    return lines
