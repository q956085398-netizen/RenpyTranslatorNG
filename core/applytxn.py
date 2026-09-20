# -*- coding: utf-8 -*-
"""应用事务器:「应用到游戏」= 暂存生成 → 快速结构检查 → 统一替换 → 应用清单/恢复点。

工单 07(ADR-0005 的应用侧;外部 tl 变更检测属工单 08):

1. **暂存生成**:把游戏 tl/<语言> 骨架复制进项目资产目录下的暂存区,在副本上回填
   译文,并生成人名与关系词显示层脚本、字体(tl 覆盖脚本 + 中文字体资产)与语言
   入口。统一应用覆盖全部项目资产——人名与关系词显示层每次都随译文刷新,不存在
   隐藏例外(字体与语言入口仍受应用设置开关控制,那是用户显式选择)。
2. **快速结构检查**:暂存区文件的结构完整性——tl 副本与骨架逐行"引号外内容"一致
   (回填只允许改变引号里的内容)、工具生成脚本的 python 块可独立编译、无控制
   字符。任何一条不过即中止,游戏目录零改动。
3. **统一替换**:先为每个受影响文件收好恢复点备份(work 目录,不在游戏安装里)、
   写事务日志(journal),再统一替换进游戏安装(含物理字体替换)。异常时按日志
   回滚;进程崩溃留下的"有日志、无清单"目录在下次应用/停用时回滚——任何阶段
   失败,游戏目录保持应用前状态,无半应用。
4. **应用清单/恢复点**:work/<项目身份>/applies/<应用ID>/manifest.json 记录每个
   文件的动作(新增/修改/移除)、归属(译文/字体/语言入口/显示层)与前后哈希,
   backup/ 保存替换前字节。restore 撤销单次应用(建议从最新开始连续撤销);
   deactivate 按全部清单从新到旧依次撤销并清理工具文件——只撤销工具管理的
   文件,汉化项目及其资产(项目库、恢复点)原样保留。

布局(均在项目资产目录,随项目完整导出/删除):
- work/<项目身份>/apply_staging/   暂存区(应用结束即删,镜像 game 相对结构)
- work/<项目身份>/applies/<应用ID>/{manifest.json, journal.json, backup/<相对路径>}
"""
import hashlib
import os
import re
import shutil
import time

from . import fontpatch, tlgen, util

STAGING_DIRNAME = "apply_staging"
APPLIES_DIRNAME = "applies"
BACKUP_DIRNAME = "backup"
MANIFEST_NAME = "manifest.json"
JOURNAL_NAME = "journal.json"

# 应用清单的归属标签:这个文件是谁生成的(用户故事 16"知道每个文件是谁生成的")
OWNER_TL = "tl"                        # 译文回填(tl/<语言> 下全部文件)
OWNER_FONT = "font"                    # 字体(tl 覆盖脚本、中文字体资产、物理替换)
OWNER_LANG_ENTRY = "lang_entry"        # 设置内语言切换入口
OWNER_TEXT_HELPERS = "text_helpers"    # 人名与关系词显示层
OWNER_LEGACY = "legacy_cleanup"        # 旧版本工具遗留文件

# 旧版本工具的显示层脚本(已由 zz_ng_text.rpy 取代):统一应用时按移除处理
LEGACY_FILES = ("zz_ng_names.rpy", "zz_ng_names.rpyc",
                "zz_ng_input.rpy", "zz_ng_input.rpyc")

# 工具命名空间里"停用汉化"要清理的游戏根文件。注意 zz_ng_dyntrans.rpy
# 不在其中:它是提取阶段写的运行时过滤器(uipatch 改写过的游戏脚本表达式
# 全部调用其 _ng_t),删掉会让游戏 NameError——它是被改写脚本的运行时依赖,
# 不属于汉化显示层,应用与停用都必须保留。
DEACTIVATE_SCRUB_ROOT_FILES = (
    "zz_ng_text.rpy", "zz_ng_text.rpyc",
    "zz_ng_language.rpy", "zz_ng_language.rpyc",
    "zz_ng_names.rpy", "zz_ng_names.rpyc",
    "zz_ng_input.rpy", "zz_ng_input.rpyc",
)


class ApplyError(RuntimeError):
    """应用事务失败(游戏目录保持应用前状态)。"""


class ApplyCheckError(ApplyError):
    """快速结构检查未通过(暂存阶段,游戏目录零改动)。"""


# ---------------------------------------------------------------- 基础工具

def _noop(_msg):
    pass


def _drop(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _rel(path):
    return path.replace(os.sep, "/")


def _at(base, rel):
    """POSIX 相对路径 -> 该基准目录下的实际路径(清单路径的唯一落点)。"""
    return os.path.join(base, *rel.split("/"))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_bytes(a, b):
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(1 << 16), fb.read(1 << 16)
            if ca != cb:
                return False
            if not ca:
                return True


def snapshot_tree(base):
    """目录内全部文件的 {相对路径(POSIX): sha256}——测试与差异核对共用。"""
    out = {}
    if not os.path.isdir(base):
        return out
    for root, _dirs, files in os.walk(base):
        for f in files:
            p = os.path.join(root, f)
            out[_rel(os.path.relpath(p, base))] = _sha256_file(p)
    return out


def _write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _new_apply_id(work):
    """时间戳应用 ID;同一秒内的第二次应用加序号,保证目录唯一。"""
    base = time.strftime("%Y%m%d-%H%M%S")
    apps = os.path.join(work, APPLIES_DIRNAME)
    apply_id, n = base, 2
    while os.path.exists(os.path.join(apps, apply_id)):
        apply_id = "%s-%d" % (base, n)
        n += 1
    return apply_id


def _prune_empty_dirs(game_base, rel_dir):
    """删除 rel_dir 子树里变空的目录(自底向上;rel_dir 本身空了也一并删除)。"""
    base = _at(game_base, rel_dir)
    if not os.path.isdir(base):
        return
    for root, _dirs, _files in os.walk(base, topdown=False):
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass


# ---------------------------------------------------------------- 快速结构检查

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STR = re.compile(r'"(?:[^"\\]|\\.)*"')
_PY_HDR = re.compile(r"^(init\b[^\n:]*\bpython\b[^\n:]*|translate\s+\S+\s+python)\s*:$")


def _structure_only(text):
    """去掉全部双引号字符串内容后的逐行结构:回填只允许改变引号里的内容。"""
    return [_STR.sub('""', ln) for ln in text.split("\n")]


def _compile_python_blocks(text, name):
    """工具生成脚本里的 init/translate python 块逐块编译,返回问题列表。"""
    problems = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if not _PY_HDR.match(lines[i]):
            i += 1
            continue
        block, j = [], i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j][0] in " \t"):
            block.append(lines[j])
            j += 1
        indents = [len(l) - len(l.lstrip(" ")) for l in block if l.strip()]
        cut = min(indents) if indents else 0
        body = "\n".join(l[cut:] if len(l) >= cut else "" for l in block)
        try:
            compile(body, "%s:%d" % (name, i + 1), "exec")
        except SyntaxError as e:
            problems.append("%s: 第 %d 行起的 python 块无法编译(%s)" % (name, i + 1, e))
        i = j
    return problems


def quick_check(staging, copied, owners):
    """暂存区快速结构检查:通过后才允许动游戏目录。

    copied 是 [(游戏骨架文件, 暂存副本)]——副本必须与骨架"引号外内容"逐行一致;
    工具生成的脚本(owners 里非 tl 归属的 .rpy)则要求 python 块可独立编译。
    """
    problems = []
    src_of = {dst: src for src, dst in copied}
    for root, _dirs, files in os.walk(staging):
        for f in sorted(files):
            if not f.endswith((".rpy", ".rpym")):
                continue
            sp = os.path.join(root, f)
            rel = _rel(os.path.relpath(sp, staging))
            with open(sp, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if _CTRL.search(text):
                problems.append("%s: 含控制字符" % rel)
            src = src_of.get(sp)
            if src is not None:
                with open(src, "r", encoding="utf-8", errors="replace") as fh:
                    stext = fh.read()
                if _structure_only(text) != _structure_only(stext):
                    problems.append("%s: 回填改变了骨架结构(引号外内容或行数变化)" % rel)
            elif owners.get(rel) != OWNER_TL:
                problems.extend(_compile_python_blocks(text, rel))
    if problems:
        raise ApplyCheckError("快速结构检查未通过(游戏目录未改动):\n  "
                              + "\n  ".join(problems[:8]))


# ---------------------------------------------------------------- 事务主体

def _fill_staged(fill_targets, text_map, key_map):
    """在暂存副本上回填译文(独立函数便于测试注入暂存阶段的破坏)。"""
    return tlgen.fill_translations(fill_targets, text_map, key_map)


def _install_file(game_base, entry):
    """把一个暂存文件替换进游戏安装(独立函数便于测试注入替换阶段的失败)。"""
    target = _at(game_base, entry["path"])
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(entry["staged"], target)


def _plan(game_base, staging, game_tl, language, owners, font_refs):
    """统一替换的文件计划:每个受影响文件的动作、归属与暂存来源。"""
    entries = []
    seen = set()
    for root, _dirs, files in os.walk(staging):
        for f in sorted(files):
            sp = os.path.join(root, f)
            rel = _rel(os.path.relpath(sp, staging))
            seen.add(rel)
            target = _at(game_base, rel)
            entries.append({"path": rel,
                            "action": "modified" if os.path.isfile(target) else "added",
                            "owner": owners.get(rel, OWNER_TL), "staged": sp})
    # tl 下不在暂存集里的文件(上次应用的 .rpyc、已从提取结果消失的旧文件)→ 移除
    for root, _dirs, files in os.walk(game_tl):
        for f in sorted(files):
            rel = "game/tl/%s/%s" % (language, _rel(os.path.relpath(os.path.join(root, f), game_tl)))
            if rel not in seen:
                entries.append({"path": rel, "action": "removed", "owner": OWNER_TL})
    # 生成脚本的陈旧编译缓存与旧版工具的显示层脚本(zz_ng_text.rpy 取代):
    # 统一应用时移除
    removals = [("game/zz_ng_text.rpyc", OWNER_TEXT_HELPERS),
                ("game/zz_ng_language.rpyc", OWNER_LANG_ENTRY)]
    removals += [("game/" + n, OWNER_LEGACY) for n in LEGACY_FILES]
    for rel, owner in removals:
        if rel in seen or not os.path.isfile(_at(game_base, rel)):
            continue
        entries.append({"path": rel, "action": "removed", "owner": owner})
    # 物理替换的游戏字体:替换前字节同样进恢复点
    for ref in font_refs:
        rel = "game/" + ref
        if rel not in seen:
            entries.append({"path": rel, "action": "modified",
                            "owner": OWNER_FONT, "font": True})
    return entries


def _replace(game_base, entries, font_refs, staged_font):
    """执行统一替换(备份与日志已就绪)。"""
    for e in entries:
        if e["action"] == "removed":
            _drop(_at(game_base, e["path"]))
        elif not e.get("font"):
            _install_file(game_base, e)
    if font_refs:
        fontpatch.replace_fonts_in_place(os.path.join(game_base, "game"),
                                         staged_font, font_refs)
    for e in entries:
        if e["action"] != "removed":
            e["after"] = _sha256_file(_at(game_base, e["path"]))
    _prune_empty_dirs(game_base, "game/tl")


def _rollback_entries(game_base, rp_dir, entries, log):
    """按事务日志/应用清单把游戏目录恢复到应用前状态(回滚与还原共用)。

    added → 删除;modified/removed → 从恢复点备份还原字节。反复执行幂等:
    文件不在了就跳过,还原内容与现状一致也只是原样覆盖。
    """
    backup_dir = os.path.join(rp_dir, BACKUP_DIRNAME)
    for e in entries:
        target = os.path.join(game_base, *e["path"].split("/"))
        try:
            if e["action"] == "added":
                _drop(target)
            else:
                src = _at(backup_dir, e["path"])
                if os.path.isfile(src):
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(src, target)
        except OSError:
            log("⚠ 回滚文件失败(可稍后从恢复点手动恢复): %s" % e["path"])
    _prune_empty_dirs(game_base, "game/tl")


def run(game_base, work, language, cfg, text_map, key_map, keep_files,
        fill_files=None, relations=(), relation_words=(), dump=None,
        untranslated=0, dropped=0, suggestions=0, external=None,
        log=None, should_stop=None):
    """执行一次事务式应用,返回可验证的任务摘要(工单 07)。

    pipeline 侧先构建任务与译文映射(出现位置同步、旧缓存导入都在应用任务内),
    这里只负责文件事务:暂存生成 → 快速结构检查 → 统一替换 → 应用清单/恢复点。
    keep_files 是统一应用管理的 tl 文件集(相对 tl/<语言> 的路径,build_jobs
    识别的当前任务清单——不在其中的 tl 文件按旧文件移除);fill_files 是其中
    要回填的子集(translate_strings 关闭时纯 strings 文件只通过不回填)。
    external 是外部译文变更的处理计数(工单 08,pipeline 传入),写进应用清单
    留痕:放弃的覆盖、导入的已并进译文映射。
    """
    log = log or _noop
    game_tl = os.path.join(game_base, "game", "tl", language)
    if not os.path.isdir(game_tl):
        raise ApplyError("tl 目录不存在,请先执行提取: %s" % game_tl)
    recover_interrupted(work, game_base, log)
    staging = os.path.join(work, STAGING_DIRNAME)
    shutil.rmtree(staging, ignore_errors=True)
    if should_stop and should_stop():
        return {"stopped": True}

    # ---- 阶段 1:暂存生成(游戏目录只读) ----
    # 暂存区镜像游戏安装根目录的相对结构(game/tl/<语言>、game/fonts、game/zz_*),
    # 清单路径与替换目标由此直接映射
    stage_game = os.path.join(staging, "game")
    stage_tl = os.path.join(stage_game, "tl", language)
    owners = {}
    copied = []      # (骨架源文件, 暂存副本):结构检查逐行比对的输入

    def tl_rel(f):
        """tl 文件引用(绝对路径或相对 tl 目录) -> 相对 tl 目录的 POSIX 路径。"""
        f = str(f)
        return _rel(os.path.relpath(f, game_tl) if os.path.isabs(f) else f)

    keep_rels = sorted({tl_rel(f) for f in keep_files})
    for rel_tl in keep_rels:
        src = _at(game_tl, rel_tl)
        if not os.path.isfile(src):
            continue
        dst = _at(stage_tl, rel_tl)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((src, dst))
        owners["game/tl/%s/%s" % (language, rel_tl)] = OWNER_TL
    fill_targets = [_at(stage_tl, tl_rel(f))
                    for f in (fill_files if fill_files is not None else keep_files)]
    filled = _fill_staged(fill_targets, text_map, key_map)

    font_path, font_refs = "", []
    if cfg.get("apply_font", True):
        font_path = cfg.get("font_path", "")
        if not font_path or not os.path.isfile(font_path):
            raise ApplyError("字体文件不存在,应用已中止(游戏目录未改动): %s" % font_path)
        name = os.path.basename(font_path)
        os.makedirs(os.path.join(stage_game, "fonts"), exist_ok=True)
        shutil.copy2(font_path, os.path.join(stage_game, "fonts", name))
        owners["game/fonts/" + name] = OWNER_FONT
        _write_text(os.path.join(stage_tl, "zz_ng_font.rpy"),
                    fontpatch.font_override_content(language, name))
        owners["game/tl/%s/zz_ng_font.rpy" % language] = OWNER_FONT
        font_refs = fontpatch.fonts_to_replace(os.path.join(game_base, "game"), name)
    if cfg.get("apply_lang_entry", True):
        _write_text(os.path.join(stage_game, "zz_ng_language.rpy"),
                    fontpatch.lang_entry_content(language))
        owners["game/zz_ng_language.rpy"] = OWNER_LANG_ENTRY
    # 人名与关系词显示层:统一应用无隐藏例外,不受任何开关限制,每次都刷新
    helpers, _mapping = fontpatch.text_helpers_content(
        game_base, language, relations, dump=dump, words=relation_words,
        tl_dir=stage_tl)
    _write_text(os.path.join(stage_game, "zz_ng_text.rpy"), helpers)
    owners["game/zz_ng_text.rpy"] = OWNER_TEXT_HELPERS

    # ---- 阶段 2:快速结构检查(全部在暂存区) ----
    quick_check(staging, copied, owners)
    if should_stop and should_stop():
        return {"stopped": True}

    # ---- 阶段 3:统一替换(先备份、写日志,再动游戏目录) ----
    entries = _plan(game_base, staging, game_tl, language, owners, font_refs)
    apply_id = _new_apply_id(work)
    rp_dir = os.path.join(work, APPLIES_DIRNAME, apply_id)
    backup_dir = os.path.join(rp_dir, BACKUP_DIRNAME)
    for e in entries:
        if e["action"] == "added":
            continue
        src = _at(game_base, e["path"])
        dst = _at(backup_dir, e["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        e["before"] = _sha256_file(src)
    # 物理字体替换会建游戏内的本地安全网目录:失败回滚时若系本次所建须一并
    # 移除,失败的尝试不在游戏目录留下任何痕迹(已提交的应用则按清单保留)
    font_backup_dir = os.path.join(game_base, "game", fontpatch._BACKUP_DIR)
    font_backup_created = font_refs and not os.path.isdir(font_backup_dir)
    # 事务日志:崩溃恢复的依据(比应用清单多记录一次"进行中")
    util.write_json(os.path.join(rp_dir, JOURNAL_NAME), {
        "apply_id": apply_id, "game_base": game_base, "language": language,
        "started_at": time.time(), "font_backup_created": bool(font_backup_created),
        "entries": [{"path": e["path"], "action": e["action"], "owner": e["owner"]}
                    for e in entries],
    })
    try:
        _replace(game_base, entries, font_refs,
                 os.path.join(staging, "game", "fonts", os.path.basename(font_path))
                 if font_refs else "")
    except Exception:
        _rollback_entries(game_base, rp_dir, entries, log)
        if font_backup_created:
            shutil.rmtree(font_backup_dir, ignore_errors=True)
        shutil.rmtree(rp_dir, ignore_errors=True)
        log("❌ 替换阶段失败,游戏目录已回滚到应用前状态")
        raise

    # ---- 应用清单 = 提交点 ----
    counts = {}
    for e in entries:
        counts[e["owner"]] = counts.get(e["owner"], 0) + 1
    external_full = dict(external or {"detected": 0, "imported": 0, "discarded": 0})
    external_full.setdefault("items", [])
    summary = {"apply_id": apply_id, "filled": filled, "untranslated": untranslated,
               "dropped": dropped, "suggestions": suggestions,
               "files_total": len(entries), "files": counts,
               "fonts_replaced": len(font_refs), "restore_point": rp_dir,
               "external": {k: external_full[k] for k in
                           ("detected", "imported", "discarded")}}
    manifest = {
        "apply_id": apply_id, "applied_at": time.time(), "language": language,
        "game_base": game_base, "project_files_note":
            "路径相对游戏安装根目录;backup/ 保存替换前字节",
        "files": [{"path": e["path"], "action": e["action"], "owner": e["owner"],
                   "before": e.get("before"), "after": e.get("after")} for e in entries],
        "summary": {k: summary[k] for k in
                    ("filled", "untranslated", "files", "fonts_replaced")},
        # 外部译文变更的逐项处理留痕(工单 08):哪些被导入、哪些被明确放弃
        "external_changes": external_full,
        "font_backup_kept": "game/fonts_ng_backup",
    }
    util.write_json(os.path.join(rp_dir, MANIFEST_NAME), manifest)
    _drop(os.path.join(rp_dir, JOURNAL_NAME))
    shutil.rmtree(staging, ignore_errors=True)
    log("✅ " + summary_message(summary))
    log("🧯 恢复点 %s(可查看差异、还原或停用汉化)" % apply_id)
    return summary


def summary_message(s):
    """应用任务摘要 -> 一句话(成功、跳过、待检查数量)。界面与日志共用一份格式。"""
    msg = ("应用完成:回填 %d 条译文(未译 %d 条显示英文),写入 %d 个文件"
           % (s["filled"], s["untranslated"], s["files_total"]))
    if s.get("fonts_replaced"):
        msg += ",物理替换字体 %d 个" % s["fonts_replaced"]
    pending = s["dropped"] + s["suggestions"]
    if pending:
        msg += ";待检查 %d 项(拒用译文 %d、迁移建议 %d)" % (
            pending, s["dropped"], s["suggestions"])
    return msg


# ---------------------------------------------------------------- 恢复点查询/还原/停用

def list_applies(work):
    """全部应用清单(新→旧)。已回滚的中断事务没有清单,自然不出现。"""
    apps = os.path.join(work, APPLIES_DIRNAME)
    out = []
    if not os.path.isdir(apps):
        return out
    for name in sorted(os.listdir(apps), reverse=True):
        m = util.read_json(os.path.join(apps, name, MANIFEST_NAME))
        if m:
            out.append(m)
    return out


def load_manifest(work, apply_id):
    return util.read_json(os.path.join(work, APPLIES_DIRNAME, str(apply_id),
                                       MANIFEST_NAME))


def recover_interrupted(work, game_base, log=None):
    """上次应用崩溃留下的半应用(有事务日志、无应用清单)回滚到应用前状态。"""
    log = log or _noop
    apps = os.path.join(work, APPLIES_DIRNAME)
    if not os.path.isdir(apps):
        return 0
    n = 0
    for name in sorted(os.listdir(apps)):
        rp = os.path.join(apps, name)
        if not os.path.isdir(rp) or os.path.isfile(os.path.join(rp, MANIFEST_NAME)):
            continue
        journal = util.read_json(os.path.join(rp, JOURNAL_NAME))
        if not journal:
            shutil.rmtree(rp, ignore_errors=True)    # 只收了备份没动游戏:空壳
            continue
        log("检测到上次应用中断(无应用清单),正在把游戏目录恢复到应用前状态…")
        _rollback_entries(game_base, rp, journal.get("entries") or [], log)
        if journal.get("font_backup_created"):
            shutil.rmtree(os.path.join(game_base, "game", fontpatch._BACKUP_DIR),
                          ignore_errors=True)
        shutil.rmtree(rp, ignore_errors=True)
        n += 1
    return n


def restore(work, game_base, apply_id, log=None):
    """按应用清单撤销一次应用(游戏目录回到该次应用之前)。

    撤销按清单精确逆向:新增的删除、修改/移除的从恢复点备份还原。多次应用后
    请从最新开始连续撤销(停用汉化即自动做完整序列);跳着还原旧点会得到
    混合状态。恢复点本身保留,可反复查看差异。
    """
    log = log or _noop
    rp_dir = os.path.join(work, APPLIES_DIRNAME, str(apply_id))
    manifest = util.read_json(os.path.join(rp_dir, MANIFEST_NAME))
    if not manifest:
        raise ApplyError("应用清单不存在: %s" % apply_id)
    _rollback_entries(game_base, rp_dir, manifest.get("files") or [], log)
    log("已撤销应用 %s(游戏目录回到该次应用之前;恢复点保留)" % apply_id)
    return {"restored": apply_id, "files": len(manifest.get("files") or [])}


def deactivate(work, game_base, cfg, language, log=None):
    """停用汉化:只撤销工具管理的文件,汉化项目及其资产原样保留。

    按全部应用清单从新到旧依次撤销(逐步回到第一次应用之前),再清理工具命名
    空间(zz_ng_*)的历史残留与物理替换的字体。zz_ng_dyntrans.rpy(运行时
    过滤器)是被 uipatch 改写脚本的运行时依赖,不属于汉化显示层,保留不删。
    项目库、译文、恢复点都在,再次「应用到游戏」即恢复汉化。
    """
    log = log or _noop
    recover_interrupted(work, game_base, log)
    n = 0
    for m in list_applies(work):
        n += restore(work, game_base, m["apply_id"], log)["files"]
    scrubbed = 0
    gamedir = os.path.join(game_base, "game")
    for name in DEACTIVATE_SCRUB_ROOT_FILES:
        p = os.path.join(gamedir, name)
        if os.path.isfile(p):
            _drop(p)
            scrubbed += 1
    for name in ("zz_ng_font.rpy", "zz_ng_font.rpyc"):
        p = os.path.join(gamedir, "tl", language, name)
        if os.path.isfile(p):
            _drop(p)
            scrubbed += 1
    # 物理替换的字体:游戏目录内备份还原(清单还原主路径之外的双保险,幂等)
    if os.path.isdir(os.path.join(gamedir, fontpatch._BACKUP_DIR)):
        fontpatch.restore_fonts(game_base, log)
    # 中文字体资产:与当前配置的字体文件内容一致才删,不按名字碰玩家的文件
    font_removed = False
    font_path = cfg.get("font_path", "")
    if font_path and os.path.isfile(font_path):
        p = os.path.join(gamedir, "fonts", os.path.basename(font_path))
        if os.path.isfile(p) and _same_bytes(p, font_path):
            _drop(p)
            font_removed = True
    _prune_empty_dirs(game_base, "game/tl")
    total = n + scrubbed + (1 if font_removed else 0)
    log("汉化已停用:撤销/清理 %d 个工具管理的文件;汉化项目与全部资产已保留" % total)
    return {"files": total, "restored_files": n, "scrubbed": scrubbed,
            "font_asset_removed": font_removed}


def diff(work, game_base, apply_id):
    """一个恢复点的差异报告:每文件的动作、归属、前后哈希与当前是否仍在该状态。"""
    m = load_manifest(work, apply_id)
    if not m:
        raise ApplyError("应用清单不存在: %s" % apply_id)
    rp_dir = os.path.join(work, APPLIES_DIRNAME, str(apply_id))
    rows = []
    for e in m.get("files") or []:
        row = dict(e)
        bp = _at(os.path.join(rp_dir, BACKUP_DIRNAME), e["path"])
        row["backup"] = bp if os.path.isfile(bp) else None
        cur = _at(game_base, e["path"])
        row["current"] = cur if os.path.isfile(cur) else None
        row["current_is_after"] = (row["current"] is not None and e.get("after")
                                   and _sha256_file(cur) == e["after"])
        rows.append(row)
    return {"manifest": m, "files": rows}
