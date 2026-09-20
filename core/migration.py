# -*- coding: utf-8 -*-
"""旧数据自动迁移（工单 04）。

旧版语义：项目数据按游戏安装的目录名存放在 work/<目录名>/（无稳定身份），
游戏库记录在 library.json，应用配置在 config.json。本模块在升级首次启动时
把它们导入注册库（core.registry，稳定项目身份）与项目库（core.project_store，
出现位置 + 译文记录）：

1. **scan**：扫描 work/ 下的旧目录与两份应用文件，按目录名匹配游戏库里的
   游戏安装（config 的 last_game 作为补充候选），生成迁移预览：计数、匹配
   状态、待检查项。目录名是已登记项目身份但项目库尚未同步的过渡期目录
   （译文仍只在旧镜像文件里）同样在列：安装关联直接来自注册库，不按名猜。
2. **apply**：先自动创建备份（work/_migration_backup/<时间戳>/），再逐项目
   登记（并入已登记项目或新建）、拷贝项目资产、同步出现位置、导入译文
   （来源 migration）。每个项目的导入作为项目任务（kind=migrate）经协调器
   原子登记——忙时跳过该项目而不冲突。任何阶段失败时整体回滚（删除新建
   项目、还原项目资产目录、撤掉已迁移标记），绝不留下半应用状态。
3. **待检查项**：无法可靠匹配的内容（无主译文、旧菜单任务键、匹配不到或
   无法唯一确定游戏安装、对不上当前出现位置结构的旧译文）进入报告的
   review_items，带来源说明，绝不静默丢弃；结构已变化但出现位置仍在的旧
   译文按 ADR-0001 保留为迁移建议（source=migration，采用须经用户确认）。
4. **幂等**：旧目录写入已迁移标记（_migrated.json），重复执行跳过；标记
   丢失（如崩溃后）时重跑也只补空位、绝不覆盖既有译文或重复登记。

游戏库记录与应用配置在新结构中继续使用（游戏库是选择游戏的入口，配置保留
可读文件），迁移不改动它们；无法解析的应用文件原样保留并成为待检查项。

匹配边界：旧目录名来自游戏安装目录的 basename（非法字符替换为 _），按
basename 大小写不敏感匹配；目录名中的 _ 无法还原原字符（A_B 可能来自 A:B），
匹配不到时进入待检查项而不是猜测。下划线开头的目录视为工具内部/调试目录
跳过。

迁移来的译文登记为未确认（来源 migration）：旧格式没有人工确认标记，无法
区分人工与模型产出，按保守语义交由用户后续确认（与工单 05 的旧镜像导入
同一约定）。已知边缘：「清空译文缓存」会清掉未确认的当前译文，旧数据仍在
原目录，删除该目录里的 _migrated.json 标记后重启即可重新迁移。
"""
import os
import re
import shutil
import time

from . import coordinator, project_store, registry, util
from .util import write_json

BACKUP_DIR_NAME = "_migration_backup"
MARKER_NAME = "_migrated.json"

# 旧 work/<目录名>/ 下的项目资产文件（translations.json 不在清单里：译文经
# import 进项目库，不再生成派生镜像文件——工单 10 收缩步骤）
ASSET_JSON_FILES = ("dump.json", "jobs.json", "relations.json", "relation_words.json",
                    "glossary.json", "ipatch.json", "ipatch_skip.json")
ASSET_DIRS = ("uipatch_backup",)

# 旧版按序号编号的菜单任务键（新系统菜单字幕统一经 strings 机制，见工单 05）
_MENU_KEY = re.compile(r":c\d+$")


def _fail_point(at, name):
    """测试接缝：在指定阶段注入失败，用于验证回滚的完整性（唯一入口）。"""
    if at and name in at:
        raise RuntimeError("注入的迁移失败：%s" % name)


def _review(key, text, source, detail):
    """一条待检查项：key 定位、text 是内容、source 是来源文件、detail 是说明。"""
    return {"key": key, "text": text, "source": source, "detail": detail}


def _load_dict(path):
    """读 JSON 对象；损坏/不存在/类型不对时返回 (None, 错误说明)。"""
    if not os.path.isfile(path):
        return None, ""
    data = util.read_json(path, None)
    if data is None:
        return None, "无法解析（不是有效的 JSON）"
    if not isinstance(data, dict):
        return None, "结构不符合预期（不是 JSON 对象）"
    return data, ""


def _load_list(path):
    if not os.path.isfile(path):
        return None, ""
    data = util.read_json(path, None)
    if data is None:
        return None, "无法解析（不是有效的 JSON）"
    if not isinstance(data, list):
        return None, "结构不符合预期（不是 JSON 数组）"
    return data, ""


# ---------- 旧目录发现与数据读取 ----------

def _legacy_dirs():
    """work/ 下可能承载旧版项目数据的目录（尚未过滤是否含数据文件）。"""
    work = os.path.join(util.app_dir(), "work")
    try:
        names = os.listdir(work)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith((".", "_")):   # 备份目录、__pycache__、调试目录
            continue
        if os.path.isdir(os.path.join(work, n)):
            out.append((n, os.path.join(work, n)))
    return out


def _store_synced(d):
    """项目资产目录里的项目库是否已同步过出现位置（无库或空库返回 False）。

    过渡期目录（工单 03–04 时代登记、项目库尚未建立或从未同步）的译文仍只在
    旧镜像文件里，必须继续走迁移；项目库一旦同步过出现位置，旧镜像在其建立
    过程中已尽数导入（import_currents 只补空位），该目录就是纯新式目录。"""
    return project_store.ProjectStore(os.path.basename(d)).occurrence_count() > 0


def _has_legacy_data(d):
    """目录里是否存在旧版项目数据文件。"""
    try:
        names = set(os.listdir(d))
    except OSError:
        return False
    return any(n in names for n in ASSET_JSON_FILES + ("translations.json",))


def _translations_of(d):
    """旧译文表：只保留携带内容的键（空值不计数、不迁移、不成待检查项）。"""
    data, _err = _load_dict(os.path.join(d, "translations.json"))
    if not data:
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str) and v}


def _valid_job_keys(jobs):
    """可建立出现位置的任务键 -> 任务。"""
    return dict(_iter_migratable_jobs(jobs))


def _iter_migratable_jobs(jobs):
    """产出可建立出现位置的任务（跳过无源文本与旧菜单键）。"""
    for j in jobs:
        if not isinstance(j, dict) or not j.get("old"):
            continue
        k = str(j.get("key", ""))
        if not k or j.get("kind") == "menu" or _MENU_KEY.search(k):
            continue
        yield k, j


def _copy_tree_contents(src, dst):
    """把 src 下全部文件按相对路径复制到 dst（不删除 dst 既有内容）。

    返回复制的文件数。"""
    n = 0
    for root, _sub, files in os.walk(src):
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dst, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            n += 1
    return n


def _occurrence_records(dump, jobs):
    """旧任务清单 -> 出现位置记录，与 pipeline._jobs 的新提取同构。

    对白任务的来源文件从 dump 取（带 game/ 前缀，与新提取的指纹一致；旧
    jobs.json 没有 src_file 字段）；strings 用任务自带的 tl 相对路径。"""
    records = []
    for _k, j in _iter_migratable_jobs(jobs):
        j = dict(j)
        if (j.get("kind") or "say") == "say" and not j.get("src_file"):
            entry = (dump or {}).get(_k.split(":")[0])
            if entry and entry.get("filename"):
                j["src_file"] = entry["filename"]
        records.append(project_store.record_from_job(j))
    return records


def _install_version(path):
    """游戏安装的本地版本；判不出或目录不存在返回 None。"""
    if not path or not os.path.isdir(path):
        return None
    try:
        from . import updates
        return updates.local_version(path, os.path.basename(os.path.normpath(path))) or None
    except Exception:
        return None


# ---------- 扫描：迁移预览 ----------

def scan():
    """扫描旧数据，生成迁移预览（只读，不改任何文件）。

    返回 {"projects": [项目预览], "library": {...}, "config": {...},
    "review_items": [应用级待检查项]}。项目预览含 match（new=新建 / merge=
    并入已登记项目 / missing=找不到安装 / ambiguous=同名安装有多个）、
    counts、plan 与项目级 review_items。
    """
    app = util.app_dir()
    lib_data, lib_err = _load_dict(os.path.join(app, "library.json"))
    lib_ok = lib_data is not None or not os.path.isfile(os.path.join(app, "library.json"))
    lib_index = {}
    n_games = 0
    if lib_data is not None:
        games = [g for g in lib_data.get("games", [])
                 if isinstance(g, dict) and isinstance(g.get("path"), str) and g["path"]]
        n_games = len(games)
        for g in games:
            base = os.path.basename(os.path.normpath(g["path"])).lower()
            lib_index.setdefault(base, [])
            p = os.path.normpath(g["path"])
            if p not in lib_index[base]:
                lib_index[base].append(p)
    cfg_data, cfg_err = _load_dict(os.path.join(app, "config.json"))
    cfg_ok = cfg_data is not None or not os.path.isfile(os.path.join(app, "config.json"))
    last_game = ""
    if cfg_data is not None and isinstance(cfg_data.get("last_game"), str):
        last_game = cfg_data["last_game"]

    review = []
    if not lib_ok:
        review.append(_review("library.json", "", os.path.join(app, "library.json"),
                              "游戏库记录无法读取：%s（原文件保留，请人工检查）" % lib_err))
    if not cfg_ok:
        review.append(_review("config.json", "", os.path.join(app, "config.json"),
                              "应用配置无法读取：%s（原文件保留，请人工检查）" % cfg_err))

    projects = []
    candidates = [(n, d) for n, d in _legacy_dirs() if _has_legacy_data(d)]
    if candidates:
        reg = registry.Registry()
        try:
            reg_by_id = {p["id"]: p for p in reg.projects()}
            for name, d in candidates:
                registered = reg_by_id.get(name)
                if registered is not None:
                    # 过渡期目录（工单 03–04 时代）：按身份登记但项目库未同步，
                    # 译文仍只在旧镜像文件里才需要迁移；没有镜像文件的已登记
                    # 目录就是普通新式项目（项目库会在首次任务时同步），不打扰
                    if (not os.path.isfile(os.path.join(d, "translations.json"))
                            or _store_synced(d)):
                        continue
                elif _store_synced(d):
                    continue    # 项目库已同步的新式目录（未登记但按身份存放）；
                                # 未登记且未同步的目录按旧布局目录名匹配——只产
                                # 生预览项，应用前有用户确认（迁移预览弹窗）兜底
                projects.append(_scan_project(name, d, lib_index, last_game, reg,
                                              registered=registered))
        finally:
            reg.close()
    return {"projects": projects, "review_items": review,
            "library": {"ok": lib_ok, "error": lib_err, "games": n_games},
            "config": {"ok": cfg_ok}}


def _scan_project(name, d, lib_index, last_game, reg, registered=None):
    trans = _translations_of(d)
    jobs, _err = _load_list(os.path.join(d, "jobs.json"))
    jobs = jobs or []
    dump, _err = _load_dict(os.path.join(d, "dump.json"))
    rel, _err = _load_list(os.path.join(d, "relations.json"))
    valid = _valid_job_keys(jobs)
    records = _occurrence_records(dump, jobs)
    fingerprints = {r["occurrence_id"]: r["fingerprint"] for r in records}

    if registered is not None:
        # 过渡期目录：身份与游戏安装关联已在注册库里，不需要（也绝不）按
        # 目录名猜测匹配；当前安装已失效时与"找不到安装"同一待遇
        install = registered.get("path")
        match = ({"status": "merge", "install_path": install, "candidates": []}
                 if install and os.path.isdir(install)
                 else {"status": "missing", "install_path": None, "candidates": []})
    else:
        cands = list(lib_index.get(name.lower(), []))
        if last_game and os.path.basename(os.path.normpath(last_game)).lower() == name.lower():
            p = os.path.normpath(last_game)
            if p not in cands:
                cands.append(p)
        if not cands:
            match = {"status": "missing", "install_path": None, "candidates": []}
        elif len(cands) > 1:
            match = {"status": "ambiguous", "install_path": None, "candidates": cands}
        else:
            install = cands[0]
            status = "merge" if reg.find_by_path(install) is not None else "new"
            match = {"status": status, "install_path": install, "candidates": []}

    trans_source = os.path.join(d, "translations.json")
    items = []
    for k, text in trans.items():
        if k in valid:
            continue
        if any(str(j.get("key", "")) == k for j in jobs):
            items.append(_review(k, text, trans_source,
                                 "旧菜单任务键：菜单字幕在新版统一经 strings 机制翻译"
                                 "（出现位置标识 S:<原文>），该译文需人工确认归属"))
        else:
            items.append(_review(k, text, trans_source,
                                 "任务清单中没有该键（更早提取的遗留数据）"))
    return {"name": name, "dir": d,
            "done": os.path.isfile(os.path.join(d, MARKER_NAME)),
            "match": match, "translations": trans, "valid_jobs": valid,
            "records": records, "fingerprints": fingerprints,
            "review_items": items,
            "counts": {"translations": len(trans),
                       "relations": len(rel) if isinstance(rel, list) else 0},
            "plan": {"translations_importable": sum(1 for k in trans if k in valid)}}


# ---------- 备份与还原 ----------

def _create_backup(projects):
    """迁移前备份：旧项目目录整目录 + library.json + config.json + 清单。"""
    work = os.path.join(util.app_dir(), "work")
    base = time.strftime("%Y%m%d-%H%M%S")
    run = os.path.join(work, BACKUP_DIR_NAME, base)
    i = 0
    while os.path.exists(run):
        i += 1
        run = os.path.join(work, BACKUP_DIR_NAME, "%s-%d" % (base, i))
    os.makedirs(run)
    app = util.app_dir()
    manifest = {"created_at": time.time(), "projects": [],
                "library": False, "config": False}
    for fname, key in (("library.json", "library"), ("config.json", "config")):
        s = os.path.join(app, fname)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(run, fname))
            manifest[key] = True
    for p in projects:
        shutil.copytree(p["dir"], os.path.join(run, "projects", p["name"]))
        manifest["projects"].append(p["name"])
    write_json(os.path.join(run, "manifest.json"), manifest)
    return run


def restore_backup():
    """从最近一次迁移备份还原旧数据文件（游戏库记录、应用配置、旧项目数据）。

    只把备份里的文件复制回原位（覆盖现有），不删除备份之外新增的文件；
    返回还原的文件数，没有可用备份返回 0。
    """
    root = os.path.join(util.app_dir(), "work", BACKUP_DIR_NAME)
    if not os.path.isdir(root):
        return 0
    try:
        runs = sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return 0
    if not runs:
        return 0
    run = os.path.join(root, runs[-1])
    app = util.app_dir()
    n = 0
    for fname in ("library.json", "config.json"):
        s = os.path.join(run, fname)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(app, fname))
            n += 1
    proot = os.path.join(run, "projects")
    if os.path.isdir(proot):
        for name in os.listdir(proot):
            sdir = os.path.join(proot, name)
            if os.path.isdir(sdir):
                n += _copy_tree_contents(sdir, os.path.join(app, "work", name))
    return n


# ---------- 回滚台账 ----------

class _Rollback:
    """失败回滚台账：迁移过程中新建/改动的位置，失败时逐一还原。

    - files：新建的零散文件（已迁移标记）——删除；
    - dirs：动过的项目资产目录——先整体删除，再把迁移前的快照复制回去；
    - projects：迁移新建的注册库项目——删除登记（并入的既有项目不动登记）。
    快照存放在备份目录的 rollback/ 子目录下，与用户可见的备份内容分开。
    """

    def __init__(self, backup_dir):
        self.backup_dir = backup_dir
        self.files = []
        self.dirs = []       # (目录, 快照目录或 None)
        self.projects = []

    def snapshot_dir(self, d):
        """记录一个即将写入的目录的迁移前内容（目录可能尚不存在）。"""
        snap = os.path.join(self.backup_dir, "rollback", "%03d" % len(self.dirs))
        n = _copy_tree_contents(d, snap) if os.path.isdir(d) else 0
        self.dirs.append((d, snap if n else None))

    def run(self, reg):
        for f in self.files:
            try:
                os.remove(f)
            except OSError:
                pass
        for d, snap in self.dirs:
            shutil.rmtree(d, ignore_errors=True)
            if snap:
                _copy_tree_contents(snap, d)
        for pid in self.projects:
            try:
                reg.delete_project(pid)
            except Exception:
                pass


# ---------- 应用：导入 ----------

def apply(log=None, _fail_at=None):
    """执行迁移，返回任务摘要报告（对账 + 待检查项）。

    任何阶段失败都会先回滚到迁移前状态再抛出异常（_fail_at 是测试注入口）。
    """
    log = log or (lambda msg: None)
    info = scan()
    report = {"backup_dir": None,
              "projects": {"created": 0, "merged": 0, "skipped": 0},
              "translations": {"total": 0, "imported": 0, "kept": 0, "review": 0},
              "review_items": list(info["review_items"]),
              "reconciled": True}
    projs = info["projects"]
    actionable = []
    for p in projs:
        if p["done"] or p["match"]["status"] not in ("new", "merge"):
            report["projects"]["skipped"] += 1
            why = ("已迁移过" if p["done"] else
                   "找不到对应的游戏安装" if p["match"]["status"] == "missing" else
                   "同名游戏安装有多个，无法自动确定：%s"
                   % "；".join(p["match"]["candidates"]))
            report["review_items"].append(_review(
                p["name"], "", p["dir"],
                "%s，暂不迁移（数据保留在原目录，条件满足后可重新迁移）" % why))
        else:
            actionable.append(p)
    if not actionable:
        return report

    _fail_point(_fail_at, "backup")
    backup_dir = _create_backup(projs)
    report["backup_dir"] = backup_dir
    log("已创建迁移备份：%s" % backup_dir)

    rb = _Rollback(backup_dir)
    reg = registry.Registry()
    try:
        try:
            for p in actionable:
                _migrate_project(p, reg, report, rb, log, _fail_at)
            _fail_point(_fail_at, "finish")
        except Exception:
            log("迁移失败，正在回滚到迁移前状态……")
            rb.run(reg)
            raise
    finally:
        reg.close()

    t = report["translations"]
    report["reconciled"] = (t["imported"] + t["kept"] + t["review"] == t["total"])
    return report


def _migrate_project(p, reg, report, rb, log, _fail_at):
    name, d = p["name"], p["dir"]
    install = p["match"]["install_path"]
    _fail_point(_fail_at, "import:%s:register" % name)
    existing = reg.find_by_path(install)
    if existing is not None:
        pid = existing["id"]
    else:
        pid = reg.create_project(name, install,
                                 version=_install_version(install))["id"]
        rb.projects.append(pid)
    store = project_store.ProjectStore(pid)
    rb.snapshot_dir(util.store_dir(pid))
    # 迁移是修改核心数据的项目任务（kind=migrate）：经协调器原子登记，
    # 同一项目已有任务在跑时跳过整个项目（数据留在原目录，稍后可重试）
    try:
        handle = coordinator.TaskCoordinator(store).begin("migrate")
    except coordinator.TaskBusy as e:
        report["projects"]["skipped"] += 1
        report["review_items"].append(_review(
            name, "", d, "项目任务《%s》正在运行，已跳过该项目"
            "（数据保留在原目录，任务结束后可重新迁移）" % e.task.get("kind")))
        return
    if existing is not None:
        report["projects"]["merged"] += 1
    else:
        report["projects"]["created"] += 1
    try:
        res = _import_project_data(p, pid, store, report, rb, _fail_at)
        summary = ("译文 导入 %d / 保留 %d / 待检查 %d"
                   % (res["imported"], res["kept"], res["review"]))
        log("已迁移《%s》：%s；%s（其余待检查项见任务摘要）"
            % (name, "并入既有项目" if existing is not None else "新建项目", summary))
        handle.finish("done", "旧数据迁移《%s》：%s" % (name, summary))
    except Exception as e:
        handle.finish("failed", "%s: %s" % (type(e).__name__, e))
        raise


def _import_project_data(p, pid, store, report, rb, _fail_at):
    """单个项目的导入主体（登记已完成，任务行由调用方收尾）。

    返回本项目的译文对账明细 {"imported", "kept", "review"}。"""
    name, d = p["name"], p["dir"]
    store_dir = util.store_dir(pid)

    # 项目资产：只补缺，绝不覆盖目标里已有的文件（并入的新式项目可能已有
    # 更新的提取结果）；被让位的旧文件仍在原目录，进待检查项说明。
    # 过渡期目录（按身份存放、只是项目库未同步）本身就是项目资产目录，
    # 源与目标相同，直接跳过拷贝
    _fail_point(_fail_at, "import:%s:assets" % name)
    if os.path.abspath(d) != os.path.abspath(store_dir):
        for fname in ASSET_JSON_FILES:
            s = os.path.join(d, fname)
            if not os.path.isfile(s):
                continue
            dst = os.path.join(store_dir, fname)
            if os.path.exists(dst):
                report["review_items"].append(_review(
                    fname, "", s, "项目资产目录已存在同名文件，保留了现有文件"
                    "（旧文件仍在原目录未动）"))
            else:
                shutil.copy2(s, dst)
        for sub in ASSET_DIRS:
            s = os.path.join(d, sub)
            if os.path.isdir(s) and not os.path.exists(os.path.join(store_dir, sub)):
                shutil.copytree(s, os.path.join(store_dir, sub))

    # 出现位置：项目已有提取数据（新式提取）时不导入旧的出现位置，译文按
    # 指纹逐条对账；项目还没有数据时以旧提取结果打底（与新提取同构，
    # 下次真实提取不会误降级迁移来的译文）
    occs = {o["occurrence_id"]: o for o in store.occurrences()}
    _fail_point(_fail_at, "import:%s:occurrences" % name)
    has_fresh = any(o["source_text"] for o in occs.values())
    if not has_fresh:
        store.sync_occurrences(p["records"])
        occs = {o["occurrence_id"]: o for o in store.occurrences()}

    # 译文：逐条判定 导入 / 保留 / 迁移建议 / 待检查（对账率 = 全部四类之和）
    _fail_point(_fail_at, "import:%s:translations" % name)
    t = report["translations"]
    imported0, kept0, review0 = t["imported"], t["kept"], t["review"]
    currents = store.currents()
    scan_items = {i["key"]: i for i in p["review_items"]}
    importable = {}
    suggestions = {}
    trans_source = os.path.join(d, "translations.json")
    for key, text in p["translations"].items():
        t["total"] += 1
        if key not in p["valid_jobs"]:
            t["review"] += 1
            if key in scan_items:
                report["review_items"].append(scan_items[key])
            continue
        o = occs.get(key)
        if o is None or not o["active"]:
            t["review"] += 1
            report["review_items"].append(_review(
                key, text, trans_source,
                "项目当前提取结果中没有该出现位置（可能游戏已更新），无法安全导入"))
            continue
        if o["fingerprint"] != p["fingerprints"].get(key):
            suggestions[key] = text
            t["review"] += 1
            report["review_items"].append(_review(
                key, text, trans_source,
                "出现位置结构已变化（源文本/说话人/来源文件与旧数据不一致），"
                "译文已存为迁移建议，确认后可采用"))
            continue
        if key in currents:
            t["kept"] += 1
            continue
        importable[key] = text
    if suggestions:
        store.record_migration_suggestions(suggestions)
    if importable:
        rep = store.import_currents(importable, source="migration")
        t["imported"] += rep["imported"]
        # 既无当前译文也无迁移建议才会走到导入；万一仍有既有记录被跳过
        #（并发降级等极端情形），按保留计数，对账依然闭合
        t["kept"] += len(importable) - rep["imported"]

    # 已迁移标记（在旧目录：数据原样保留，重复执行幂等）。译文记录不再生成
    # 派生镜像文件——项目库是唯一可信来源（工单 10 收缩步骤）
    _fail_point(_fail_at, "import:%s:marker" % name)
    marker = os.path.join(d, MARKER_NAME)
    write_json(marker, {"project_id": pid, "migrated_at": time.time(),
                        "translations": len(p["translations"])})
    rb.files.append(marker)
    return {"imported": t["imported"] - imported0,
            "kept": t["kept"] - kept0,
            "review": t["review"] - review0}
