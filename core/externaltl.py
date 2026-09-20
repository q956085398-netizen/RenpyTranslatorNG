# -*- coding: utf-8 -*-
"""外部 tl 变更检测（ADR-0005、工单 08）。

外部译文变更 = 用户或其他工具在汉化工作台之外对游戏 tl 文件作出的修改
（CONTEXT.md）。应用到游戏前必须检测，提供逐项比较、导入或明确放弃，
绝不静默覆盖；导入的译文经 set_human_translation 登记为人工来源、受人工
译文保护（ADR-0003）。

**基线**：每次应用成功后（pipeline 侧）扫描 game/tl/<语言>/ 全部 .rpy，保存
{文件: {sha256, 出现位置 -> 实际写入的译文与原文锚点}}，表示"工具最后写进游戏
的状态"，随每次应用自动更新、无需手工维护。检测的参照物是基线而不是项目库
当前译文——用户自己改了项目库还没应用的项目草稿不是外部变更，不该被打扰。

基线是与应用清单（manifest.json）同目录同性质的文件事务产物，放在项目资产
目录而不是项目库里：它记录"上一次应用把什么写进了游戏"，可以从应用清单 +
重新扫描推导，而项目库（ADR-0004）始终是译文记录的唯一可信来源。

**工具自身动作不误报**：恢复点还原、停用汉化、崩溃在提交点附近的应用都会改写
游戏 tl。检测时按全部应用清单对账：当前文件字节与某个"工具已知状态"（任一次
应用的前态或后态哈希）一致，就不算外部变更。两个已知代价（都是有意取舍）：

- 用户明确放弃过的外部内容，之后以完全相同的字节再次出现时不再重新打扰
  （放弃是对那份内容的决定，不是一次性的）；
- 工具创建过的文件（字体覆盖脚本等生成产物）被删除不报——它们不承载用户
  成果，每次应用都会重新生成。

**首次应用前**（还没有基线）：以"翻译/回填步骤实际写进游戏 tl 的内容"为参照
（pipeline 按项目库当前译文展开计算——第 5 步翻译会先把译文回填进游戏 tl，
那也是工具自身的写入；未译状态就是原文本身）。游戏 tl 里与之不同的内容即
外部译文：这让"游戏自带/其他工具已有的 tl 译文"在第一次应用前也能被发现并
导入，而不是被静默覆盖。

**结构对账**：外部在块内插行/删行会让注释与代码行的位置配对整体错位，逐条
导入可能把译文安到错误的出现位置上。基线记录了每个位置的原文锚点，扫描出的
锚点与基线不一致即判定该文件结构被外部改动，整体只提供放弃（应用会按当前
任务清单重新回填），不提供逐项导入。

写路径只有 set_human_translation（经调用方传入的项目库）；本模块不改游戏目录。
"""
import hashlib
import os
import time

from . import applytxn, tlgen, util

BASELINE_NAME = "tl_baseline.json"

KIND_MODIFIED = "modified"        # 外部改写了工具已回填的译文
KIND_ADDED = "added"              # 外部给未译行补了译文（或首次应用前的既有译文）
KIND_REMOVED = "removed"          # 外部删掉了译文行
KIND_UNMANAGED = "unmanaged"      # 出现位置不在当前任务清单里（无法导入）
KIND_STRUCTURE = "structure"      # 文件结构被外部改动，位置配对错位（无法导入）
KIND_REMOVED_FILE = "removed_file"    # 基线里的文件被外部删除
KIND_UNTRACKED = "untracked"      # tl 目录里出现了工具不管理的新 .rpy


class ExternalChangesError(RuntimeError):
    """检测到外部译文变更而调用方没有确认通道：应用被阻止（绝不静默覆盖）。"""

    def __init__(self, message, items=()):
        super().__init__(message)
        self.items = list(items)


def baseline_path(work):
    return os.path.join(work, BASELINE_NAME)


def load_baseline(work):
    """上次应用基线；从未应用过返回 None。"""
    data = util.read_json(baseline_path(work))
    if not data or not isinstance(data.get("files"), dict):
        return None
    return data


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(base, path):
    return os.path.relpath(path, base).replace(os.sep, "/")


def refresh_baseline(work, game_base, language):
    """把基线更新为游戏 tl 目录的当前状态（每次应用成功/还原/停用后由 pipeline 调用）。"""
    files = {}
    tl_dir = os.path.join(game_base, "game", "tl", language)
    if os.path.isdir(tl_dir):
        for root, _dirs, names in os.walk(tl_dir):
            for name in sorted(names):
                if not name.endswith(".rpy"):
                    continue
                path = os.path.join(root, name)
                files[_rel(tl_dir, path)] = {"sha256": _sha256_file(path),
                                             "keys": tlgen.scan_translations(path)}
    util.write_json(baseline_path(work),
                    {"language": language, "saved_at": time.time(), "files": files})
    return files


def _manifest_states(work, language):
    """全部应用清单里工具已知的 tl 文件状态。

    返回 ({清单路径: {该文件出现过的全部哈希}}, {工具使其缺席的清单路径})：
    前者是"工具写进过游戏的内容"（应用后态、还原后的前态）；后者是历史上被
    工具新增过或按孤儿文件移除过的路径——工具创建的文件（字体覆盖脚本等生成
    产物）被还原/停用删除是工具自身行为，缺席不算外部变更（其内容每次应用
    都重新生成，也不承载用户成果）。
    """
    prefix = "game/tl/%s/" % language
    hashes, absent = {}, set()
    for m in applytxn.list_applies(work):
        for e in m.get("files") or []:
            rel = e.get("path") or ""
            if not rel.startswith(prefix):
                continue
            if e.get("action") in ("added", "removed"):
                absent.add(rel)
                if e.get("action") == "removed":
                    continue
            hs = hashes.setdefault(rel, set())
            for h in (e.get("before"), e.get("after")):
                if h:
                    hs.add(h)
    return hashes, absent


def _emit(items, **kw):
    kw["id"] = len(items)
    items.append(kw)


def detect_changes(game_base, work, language, jobs, draft_expected=None):
    """应用前检测外部译文变更，返回 {"items": [变更项…]}。

    jobs 是当前任务清单（tlgen.build_jobs 的输出，含 ipatch 覆盖），提供出现
    位置的原文锚点（未译状态的期望值）与"位置是否仍受工具管理"。

    从未应用过（无基线）时以 draft_expected 为参照（pipeline 按"翻译/回填
    步骤实际写进游戏 tl 的内容"展开计算——第 5 步翻译会先把项目库译文回填进
    游戏 tl，那也是工具自身的写入），只报告任务清单内位置上的外部译文，
    不推断文件级变更。
    """
    items = []
    tl_dir = os.path.join(game_base, "game", "tl", language)
    if not os.path.isdir(tl_dir):
        return {"items": items}
    base = load_baseline(work)
    has_baseline = base is not None and bool(base.get("files"))
    base_files = (base.get("files") or {}) if base else {}
    anchors = {}
    for j in jobs or []:
        if j.get("old"):
            anchors.setdefault((j.get("file") or "").replace(os.sep, "/"), {}) \
                  [j["key"]] = (j.get("orig") or j["old"])
    known_hashes, known_absent = _manifest_states(work, language)

    current_rels = set()
    for root, _dirs, names in os.walk(tl_dir):
        for name in sorted(names):
            if not name.endswith(".rpy"):
                continue
            path = os.path.join(root, name)
            rel = _rel(tl_dir, path)
            current_rels.add(rel)
            entry = base_files.get(rel)
            if has_baseline and entry is None:
                # 工具没管理过的 .rpy 出现在 tl 目录里（上次应用之后外部放进来的）
                _emit(items, key=None, file=rel, kind=KIND_UNTRACKED, anchor=None,
                      baseline=None, current=None, importable=False)
                continue
            sha = _sha256_file(path)
            if entry and entry.get("sha256") == sha:
                continue        # 与基线一致：该文件没有外部变更
            mrel = "game/tl/%s/%s" % (language, rel)
            if sha in known_hashes.get(mrel, ()):
                continue        # 工具已知状态（自身应用/还原的产物），不是外部变更
            file_anchors = anchors.get(rel, {})
            scanned = tlgen.scan_translations(path)
            if has_baseline:
                base_keys = entry.get("keys") or {} if entry else {}
                # 结构对账:注释锚点错位(外部改/插/删注释行),或代码行被插删后
                # 译文序列整体旋转(某位置的译文变成前一位置的基线值,连续出现)
                seq = [k for k in scanned if k in base_keys]
                anchor_shift = any(base_keys[k].get("anchor") != scanned[k]["anchor"]
                                   for k in seq if scanned[k]["anchor"] is not None)
                rotated = sum(1 for a, b in zip(seq, seq[1:])
                              if scanned[b]["text"] is not None
                              and scanned[b]["text"] == base_keys[a]["text"])
                if anchor_shift or rotated >= 2:
                    # 逐条导入会把译文安到错误的出现位置上,整个文件只提供放弃
                    _emit(items, key=None, file=rel, kind=KIND_STRUCTURE, anchor=None,
                          baseline=None, current=None, importable=False,
                          note="该文件的行结构与上次应用不一致（外部插行/删行），"
                               "位置配对不可靠：放弃后本次应用按当前任务清单重新回填")
                else:
                    for key in sorted(set(base_keys) | set(scanned) | set(file_anchors)):
                        expected = base_keys[key]["text"] if key in base_keys \
                            else file_anchors.get(key)
                        info = scanned.get(key)
                        text = info["text"] if info else None
                        # strings 块未译的空 new 行等于"还是原文"，不是变更
                        if text == "" and expected == file_anchors.get(key):
                            continue
                        if expected == text:
                            continue
                        if text is None:
                            kind = KIND_REMOVED
                        elif expected is None:
                            kind = KIND_UNMANAGED
                        elif expected == file_anchors.get(key):
                            kind = KIND_ADDED
                        else:
                            kind = KIND_MODIFIED
                        _emit(items, key=key, file=rel, kind=kind,
                              anchor=file_anchors.get(key), baseline=expected,
                              current=text,
                              importable=kind in (KIND_MODIFIED, KIND_ADDED)
                              and key in file_anchors)
            else:
                # 从未应用过：以"翻译/回填步骤实际写入的内容"为参照，
                # 只有任务清单里的位置可判定
                drafts = draft_expected or {}
                for key, info in scanned.items():
                    if key not in file_anchors:
                        continue
                    text = info["text"]
                    expected = drafts.get(key, file_anchors[key])
                    if text == "" or text == file_anchors[key] or text == expected:
                        continue    # 未译状态（原文/空行）或工具自身写入
                    _emit(items, key=key, file=rel,
                          kind=KIND_ADDED if expected == file_anchors[key]
                          else KIND_MODIFIED,
                          anchor=file_anchors[key], baseline=expected,
                          current=text, importable=True)
    if has_baseline:
        for rel in sorted(set(base_files) - current_rels):
            mrel = "game/tl/%s/%s" % (language, rel)
            if mrel in known_absent:
                continue        # 工具使其缺席（还原新增文件 / 应用清掉孤儿文件）
            _emit(items, key=None, file=rel, kind=KIND_REMOVED_FILE, anchor=None,
                  baseline=None, current=None, importable=False)
    return {"items": items}
