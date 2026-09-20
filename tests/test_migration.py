# -*- coding: utf-8 -*-
"""工单 04 验收测试：旧数据自动迁移。

旧版数据 = work/<游戏安装目录名>/ 下的 JSON 项目数据（dump/jobs/translations/
relations）+ library.json + config.json。迁移把它们导入注册库（稳定项目身份）
与项目库（出现位置 + 译文记录），带预览、备份、对账与幂等保证。这里的断言
全部落在对外行为上：

- 迁移前可预览，apply 自动创建备份，失败后系统状态与迁移前一致、可从备份恢复；
- 迁移前后项目数量、译文数量可对账（对账率 100%，一条不多记、一条不丢）；
- 重复执行迁移不创建重复项目或重复译文记录（幂等）；
- 无法可靠匹配的内容（无主译文、旧菜单键、匹配不到游戏安装）成为待检查项，
  带有来源说明，绝不静默丢弃；
- 游戏库记录与应用配置原样保留并可在新结构中正常使用；
- 迁移产生出现位置记录与"新提取"同构：迁移后按新流程再次同步不会把迁移译文
  误降级为迁移建议。
"""
import json
import os

import pytest

from core import config as appconfig
from core import library as gamelib
from core import migration, project_store, registry, util


# ---------- 夹具：旧版数据布局 ----------

DUMP = {
    "blk_aaaa1111": {"filename": "game/script.rpy", "lineno": 10,
                     "nodes": [{"type": "say", "who": "e", "what": "Hello."},
                               {"type": "say", "who": "e", "what": "How are you?"}]},
    "blk_bbbb2222": {"filename": "game/script.rpy", "lineno": 20,
                     "nodes": [{"type": "menu", "captions": ["Leave", "Stay"]}]},
}
JOBS = [
    {"key": "blk_aaaa1111", "kind": "say", "file": "script.rpy", "old": "Hello.",
     "who": "e", "ctx": [[], []]},
    {"key": "blk_aaaa1111:s1", "kind": "say", "file": "script.rpy", "old": "How are you?",
     "who": "e", "ctx": [[], []]},
    {"key": "S:Leave", "kind": "string", "file": "screens.rpy", "old": "Leave",
     "who": "", "ctx": [[], []]},
    {"key": "S:Stay", "kind": "string", "file": "screens.rpy", "old": "Stay",
     "who": "", "ctx": [[], []]},
    # 旧版按序号编号的菜单任务键：新系统已改经 strings 机制，属于死键
    {"key": "blk_bbbb2222:c0", "kind": "menu", "file": "script.rpy", "old": "Leave",
     "who": "", "ctx": [[], []]},
]
TRANS = {"blk_aaaa1111": "你好。", "blk_aaaa1111:s1": "最近怎么样？",
         "S:Leave": "离开", "S:Stay": "留下",
         "blk_bbbb2222:c0": "离开（旧菜单）", "dead_key": "无主译文"}
REL = [{"speaker": "e", "name": "Elie", "name_cn": "艾莉", "gender": "女",
        "relation": "主角本人", "note": ""}]

GAME_NAME = "My Game-1.0-pc"


def make_legacy(app, name=GAME_NAME, dump=DUMP, jobs=JOBS, trans=TRANS, rel=REL):
    d = os.path.join(str(app), "work", name)
    os.makedirs(d, exist_ok=True)
    writes = [("dump.json", dump), ("jobs.json", jobs),
              ("translations.json", trans), ("relations.json", rel)]
    for fname, data in writes:
        if data is not None:
            with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
    return d


def make_appdata(app, games=(GAME_NAME,), last_game=True):
    """library.json + config.json：games 项为目录名（放在 games/ 下）或安装绝对路径。"""
    root = str(app)
    entries = []
    for g in games:
        p = g if os.path.isabs(g) else os.path.join(root, "games", g)
        entries.append({"path": p, "name": os.path.basename(p), "cover": "",
                        "translated": False, "added_by": "scan"})
    with open(os.path.join(root, "library.json"), "w", encoding="utf-8") as f:
        json.dump({"scan_roots": [os.path.join(root, "games")], "games": entries}, f)
    cfg = {"language": "chinese", "api_key": "sk-test"}
    if last_game and games:
        cfg["last_game"] = entries[0]["path"]
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(util, "app_dir", lambda: root)
    os.makedirs(os.path.join(root, "work"))
    return tmp_path


@pytest.fixture
def reg(app):
    r = registry.Registry(os.path.join(str(app), "registry.db"))
    try:
        yield r
    finally:
        r.close()


def store_of(reg, install_path):
    proj = reg.find_by_path(install_path)
    assert proj is not None
    return project_store.ProjectStore(proj["id"]), proj


# ---------- 预览：扫描、匹配与计数 ----------

def test_scan_finds_legacy_project_and_matches_install_by_basename(app):
    make_legacy(app)
    make_appdata(app)
    scan = migration.scan()
    assert [p["name"] for p in scan["projects"]] == [GAME_NAME]
    p = scan["projects"][0]
    assert p["match"]["status"] == "new"
    assert p["match"]["install_path"] == os.path.join(str(app), "games", GAME_NAME)
    assert p["counts"]["translations"] == len(TRANS)
    assert p["counts"]["relations"] == len(REL)
    assert scan["library"]["games"] == 1 and scan["config"]["ok"]


def test_scan_review_items_cover_dead_and_menu_keys_with_source(app):
    make_legacy(app)
    make_appdata(app)
    p = migration.scan()["projects"][0]
    items = {i["key"]: i for i in p["review_items"]}
    # 无主译文：键不在任务清单中，待检查项带来源（出自哪个文件）
    assert "dead_key" in items
    assert items["dead_key"]["source"].endswith("translations.json")
    assert items["dead_key"]["text"] == "无主译文"
    # 旧菜单键：说明新系统经 strings 机制覆盖
    assert "blk_bbbb2222:c0" in items
    assert "strings" in items["blk_bbbb2222:c0"]["detail"]
    # 可导入的译文 = 出现位置可对账的 4 条
    assert p["plan"]["translations_importable"] == 4


def test_scan_report_missing_match_when_no_install_found(app):
    make_legacy(app)
    make_appdata(app, games=("Other Game-pc",), last_game=False)
    p = migration.scan()["projects"][0]
    assert p["match"]["status"] == "missing"
    assert p["match"]["install_path"] is None


def test_scan_report_ambiguous_match_for_same_basename(app):
    make_legacy(app)
    a = os.path.join(str(app), "games1", GAME_NAME)
    b = os.path.join(str(app), "games2", GAME_NAME)
    make_appdata(app, games=[a, b], last_game=False)
    p = migration.scan()["projects"][0]
    assert p["match"]["status"] == "ambiguous"
    assert sorted(p["match"]["candidates"]) == sorted([a, b])


def test_scan_skips_new_style_store_dirs_and_backup_dir(app, reg):
    # 已注册项目按身份存放、项目库已同步出现位置的目录不是旧数据
    proj = reg.create_project("New", os.path.join(str(app), "g1"))
    store = util.store_dir(proj["id"])
    with open(os.path.join(store, "translations.json"), "w", encoding="utf-8") as f:
        json.dump({"S:New": "新"}, f)
    project_store.ProjectStore(proj["id"]).sync_occurrences(
        [project_store.record_from_job({"key": "S:New", "kind": "string",
                                        "old": "New", "who": "",
                                        "file": "screens.rpy"})])
    # 备份目录里即使有旧格式文件也不是待迁移项目
    bdir = os.path.join(str(app), "work", migration.BACKUP_DIR_NAME, "x", "Old-pc")
    os.makedirs(bdir)
    with open(os.path.join(bdir, "translations.json"), "w", encoding="utf-8") as f:
        json.dump({"k": "v"}, f)
    assert migration.scan()["projects"] == []


def test_scan_ignores_registered_dir_without_mirror_file(app, reg):
    # 已登记但项目库未同步、也没有旧镜像文件的目录是普通新式项目：
    # 没有只存在于镜像里的译文，迁移不打扰（项目库会在首次任务时同步）
    install = os.path.join(str(app), "g2")
    proj = reg.create_project("Plain", install)
    d = util.store_dir(proj["id"])
    with open(os.path.join(d, "dump.json"), "w", encoding="utf-8") as f:
        json.dump({"blk_x": {"filename": "game/script.rpy", "lineno": 1,
                             "nodes": []}}, f)
    assert migration.scan()["projects"] == []


def test_scan_and_apply_cover_transition_dir_registered_without_store(app, reg):
    """过渡期目录（工单 03–04 时代）：已按身份登记、项目库从未同步，译文仍
    只在旧镜像文件里——迁移照常导入（历史数据只经迁移进入新存储，工单 10），
    游戏安装关联直接来自注册库，绝不按目录名匹配。"""
    install = os.path.join(str(app), "games", GAME_NAME)
    os.makedirs(install)
    proj = registry.ensure_project(install, GAME_NAME, reg=reg)
    make_legacy(app, name=proj["id"])   # 旧式数据放在按身份命名的目录里，无项目库

    p = migration.scan()["projects"][0]
    assert p["match"]["status"] == "merge"
    assert p["match"]["install_path"] == install

    report = migration.apply()
    assert report["projects"]["merged"] == 1 and report["projects"]["created"] == 0
    store = project_store.ProjectStore(proj["id"])
    cur = store.currents()
    assert cur["S:Leave"] == "离开" and len(cur) == 4
    # 目录本身就是项目资产目录：不产生"同名文件让位"待检查项（源=目标跳过拷贝）
    assert not any("同名文件" in i["detail"] for i in report["review_items"])
    assert report["reconciled"] is True
    # 幂等：项目库已同步，之后的扫描不再把它当待迁移目录
    assert migration.scan()["projects"] == []


def test_scan_dir_with_only_translations_becomes_review_items(app):
    make_legacy(app, "mygame", dump=None, jobs=None,
                trans={"kA": "甲", "kB": "乙"}, rel=None)
    make_appdata(app, games=("mygame",))
    p = migration.scan()["projects"][0]
    assert p["match"]["status"] == "new"
    assert p["plan"]["translations_importable"] == 0
    assert {i["key"] for i in p["review_items"]} == {"kA", "kB"}
    assert all(i["source"] for i in p["review_items"])


# ---------- 应用：导入、对账与恢复点 ----------

def test_apply_registers_project_imports_assets_and_reconciles(app, reg):
    from core import coordinator
    legacy = make_legacy(app)
    make_appdata(app)
    report = migration.apply()
    install = os.path.join(str(app), "games", GAME_NAME)
    proj = reg.find_by_path(install)
    assert proj is not None and proj["name"] == GAME_NAME

    store = project_store.ProjectStore(proj["id"])
    # 迁移本身作为项目任务登记（修改核心数据须经协调器，工单 06 语义）
    history = coordinator.TaskCoordinator(store).history()
    assert any(t["kind"] == "migrate" and t["state"] == "done" for t in history)
    # 译文逐条对账：可匹配的 4 条全部成为当前译文，内容一致
    cur = store.currents()
    for key in ("blk_aaaa1111", "blk_aaaa1111:s1", "S:Leave", "S:Stay"):
        assert cur[key] == TRANS[key]
    assert len(cur) == 4
    # 出现位置与译文记录可对账，来源标记为迁移
    assert store.stats()["occurrences"] == 4
    assert store.stats()["currents"] == 4
    got = store.get_current("blk_aaaa1111")
    assert got["source"] == "migration" and not got["confirmed"]

    # 对账报告：total = imported + review，一条不丢
    t = report["translations"]
    assert t["total"] == 6 and t["imported"] == 4 and t["review"] == 2
    assert report["reconciled"] is True
    # 项目数量对账
    assert report["projects"]["created"] == 1
    assert len(reg.projects()) == 1
    # 项目资产（人物关系、提取结果等）进入按身份存放的项目资产目录
    new_store = util.store_dir(proj["id"])
    with open(os.path.join(new_store, "relations.json"), encoding="utf-8") as f:
        assert json.load(f) == REL
    assert os.path.isfile(os.path.join(new_store, "dump.json"))
    # 译文记录不再生成派生镜像文件：项目库是唯一可信来源（工单 10）
    assert not os.path.isfile(os.path.join(new_store, "translations.json"))
    # 旧数据原样保留，并留下已迁移标记
    with open(os.path.join(legacy, "translations.json"), encoding="utf-8") as f:
        assert json.load(f) == TRANS
    assert os.path.isfile(os.path.join(legacy, migration.MARKER_NAME))


def test_apply_is_idempotent_second_run_creates_no_duplicates(app, reg):
    make_legacy(app)
    make_appdata(app)
    first = migration.apply()
    install = os.path.join(str(app), "games", GAME_NAME)
    store, proj = store_of(reg, install)
    before = store.currents()
    second = migration.apply()
    # 第二次：全部因已迁移被跳过，不再产生任何新项目/新记录
    assert second["projects"]["created"] == 0
    assert second["projects"]["skipped"] == 1
    assert second["translations"]["imported"] == 0
    assert store_of(reg, install)[0].currents() == before
    assert len(reg.projects()) == 1
    # 即使删掉标记重跑（如中途崩溃后的手动恢复），也不产生重复数据
    os.remove(os.path.join(str(app), "work", GAME_NAME, migration.MARKER_NAME))
    third = migration.apply()
    assert third["projects"]["created"] == 0
    assert third["translations"]["imported"] == 0
    assert third["translations"]["kept"] == 4
    assert store_of(reg, install)[0].currents() == before
    assert len(reg.projects()) == 1


def test_apply_creates_backup_and_restore_brings_files_back(app, reg):
    make_legacy(app)
    make_appdata(app)
    migration.apply()
    backups = os.path.join(str(app), "work", migration.BACKUP_DIR_NAME)
    runs = os.listdir(backups)
    assert len(runs) == 1
    bdir = os.path.join(backups, runs[0])
    # 备份涵盖旧项目数据、游戏库记录与应用配置，并有清单
    assert os.path.isfile(os.path.join(bdir, "manifest.json"))
    assert os.path.isfile(os.path.join(bdir, "library.json"))
    assert os.path.isfile(os.path.join(bdir, "config.json"))
    assert os.path.isfile(os.path.join(bdir, "projects", GAME_NAME, "translations.json"))

    # 模拟迁移后的意外损坏：从备份恢复可找回原文件
    legacy_trans = os.path.join(str(app), "work", GAME_NAME, "translations.json")
    with open(legacy_trans, "w", encoding="utf-8") as f:
        json.dump({"broken": True}, f)
    n = migration.restore_backup()
    assert n >= 4
    with open(legacy_trans, encoding="utf-8") as f:
        assert json.load(f) == TRANS


def test_backup_created_before_any_import_when_apply_fails(app, reg):
    make_legacy(app)
    make_appdata(app)
    with pytest.raises(RuntimeError):
        migration.apply(_fail_at=["import:%s:register" % GAME_NAME])
    backups = os.path.join(str(app), "work", migration.BACKUP_DIR_NAME)
    assert os.listdir(backups), "失败前已创建备份"
    # 失败不产生任何新项目（可从备份恢复到迁移前状态）
    assert reg.projects() == []


def test_failure_at_any_stage_leaves_state_as_before_migration(app, reg):
    make_legacy(app)
    make_appdata(app)
    install = os.path.join(str(app), "games", GAME_NAME)
    legacy_dir = os.path.join(str(app), "work", GAME_NAME)
    stages = ["backup", "import:%s:assets" % GAME_NAME,
              "import:%s:occurrences" % GAME_NAME,
              "import:%s:translations" % GAME_NAME,
              "import:%s:marker" % GAME_NAME, "finish"]
    for stage in stages:
        with pytest.raises(RuntimeError):
            migration.apply(_fail_at=[stage])
        # 注册库、项目库、旧数据目录都保持迁移前状态（无半应用）
        assert reg.projects() == [], stage
        assert reg.find_by_path(install) is None, stage
        assert not os.path.isfile(os.path.join(legacy_dir, migration.MARKER_NAME)), stage
        assert not os.path.isdir(os.path.join(str(app), "work",
                                              "some-project-store")), stage
    # 故障恢复：不再注入失败后迁移正常完成
    report = migration.apply()
    assert report["reconciled"] is True
    assert len(reg.projects()) == 1


def test_apply_merges_into_already_registered_project(app, reg):
    """升级后先打开过一次游戏（新项目已登记、资产为空）再迁移：并入既有项目。"""
    make_legacy(app)
    make_appdata(app)
    install = os.path.join(str(app), "games", GAME_NAME)
    reg.create_project(GAME_NAME, install)
    report = migration.apply()
    assert report["projects"]["created"] == 0
    assert report["projects"]["merged"] == 1
    store, proj = store_of(reg, install)
    cur = store.currents()
    assert cur["blk_aaaa1111"] == TRANS["blk_aaaa1111"]
    assert len(cur) == 4


def test_apply_does_not_overwrite_newer_extraction_in_merged_project(app, reg):
    """并入的项目已按新流程提取过：出现位置以新数据为准，源文本对不上的旧译文
    进入待检查项，绝不覆盖新数据（ADR-0001：译文只有在出现位置仍匹配时才继承）。"""
    make_legacy(app)
    make_appdata(app)
    install = os.path.join(str(app), "games", GAME_NAME)
    proj = reg.create_project(GAME_NAME, install)
    store = project_store.ProjectStore(proj["id"])
    # 新流程提取结果：同一个出现位置，源文本已改写（游戏更新）
    fresh = [{"occurrence_id": "blk_aaaa1111", "kind": "say",
              "source_text": "Hello there.", "trans_source": "Hello there.",
              "who": "e", "source_file": "game/script.rpy",
              "fingerprint": project_store.fingerprint("say", "game/script.rpy", "e",
                                                       "Hello there.")}]
    store.sync_occurrences(fresh)
    store.record_model_result("blk_aaaa1111", "新提取的译文")
    report = migration.apply()
    assert report["projects"]["merged"] == 1
    cur = store.currents()
    # 新提取的出现位置不被旧数据改写
    assert cur["blk_aaaa1111"] != TRANS["blk_aaaa1111"]
    assert cur["blk_aaaa1111"] == "新提取的译文"
    # 出现位置仍存在但结构已变的旧译文：按 ADR-0001 保留为迁移建议（带迁移来源）
    sug = store.suggestions()
    assert len(sug) == 1
    assert sug[0]["occurrence_id"] == "blk_aaaa1111"
    assert sug[0]["text"] == TRANS["blk_aaaa1111"]
    assert sug[0]["source"] == "migration"
    # 旧译文的键对应的出现位置在最新提取中不存在 → 待检查项，不静默丢弃
    keys = {i["key"] for i in report["review_items"]}
    assert "blk_aaaa1111:s1" in keys
    assert "S:Leave" in keys


def test_migration_does_not_demote_on_next_fresh_extraction(app, reg):
    """迁移产生的出现位置与"新提取"同构：按新流程的任务清单再次同步，
    迁移来的当前译文不被误降级（对账率在迁移之后依然成立）。"""
    make_legacy(app)
    make_appdata(app)
    migration.apply()
    store, proj = store_of(reg, os.path.join(str(app), "games", GAME_NAME))
    # 模拟新提取：任务清单与 dump/jobs 同源（新提取会带 src_file），经
    # project_store.record_from_job 生成出现位置记录
    fresh_jobs = []
    for j in JOBS:
        if j["kind"] == "say":
            fresh_jobs.append(dict(j, src_file="game/script.rpy"))
        elif j["kind"] == "string":
            fresh_jobs.append(dict(j))
    records = [project_store.record_from_job(j) for j in fresh_jobs]
    rep = store.sync_occurrences(records)
    assert rep["changed"] == 0 and rep["demoted"] == 0
    cur = store.currents()
    assert cur["blk_aaaa1111"] == TRANS["blk_aaaa1111"]
    assert cur["S:Leave"] == TRANS["S:Leave"]


def test_apply_skips_project_with_running_task(app, reg):
    """同一项目已有修改核心数据的项目任务在运行：迁移跳过该项目而不是冲突。"""
    from core import coordinator
    make_legacy(app)
    make_appdata(app)
    install = os.path.join(str(app), "games", GAME_NAME)
    proj = reg.create_project(GAME_NAME, install)
    handle = coordinator.TaskCoordinator(
        project_store.ProjectStore(proj["id"])).begin("translate")
    try:
        report = migration.apply()
        assert report["projects"]["skipped"] == 1
        assert any("运行" in i["detail"] for i in report["review_items"])
        assert project_store.ProjectStore(proj["id"]).currents() == {}
    finally:
        handle.finish()


# ---------- 游戏库记录与应用配置：保留且可用 ----------

def test_library_and_config_preserved_and_usable(app, reg, monkeypatch):
    make_legacy(app)
    make_appdata(app)
    lib_path = os.path.join(str(app), "library.json")
    cfg_path = os.path.join(str(app), "config.json")
    with open(lib_path, encoding="utf-8") as f:
        before = f.read()
    with open(cfg_path, encoding="utf-8") as f:
        cfg_before = f.read()
    migration.apply()
    with open(lib_path, encoding="utf-8") as f:
        assert f.read() == before
    with open(cfg_path, encoding="utf-8") as f:
        assert f.read() == cfg_before
    # 新结构中照常可用：游戏库条目、扫描根目录与应用配置原样读出
    monkeypatch.setattr(gamelib, "LIB_PATH", lib_path)
    lib = gamelib.Library()
    assert len(lib.games) == 1 and lib.games[0]["name"] == GAME_NAME
    assert lib.scan_roots == [os.path.join(str(app), "games")]
    monkeypatch.setattr(appconfig, "CONFIG_PATH", cfg_path)
    cfg = appconfig.Config()
    assert cfg["api_key"] == "sk-test" and cfg["language"] == "chinese"


def test_unreadable_library_or_config_becomes_review_item(app):
    make_legacy(app)
    with open(os.path.join(str(app), "library.json"), "w", encoding="utf-8") as f:
        f.write("{broken json")
    scan = migration.scan()
    assert scan["library"]["ok"] is False
    assert scan["library"]["error"]
    assert scan["review_items"], "损坏的游戏库记录进入待检查项而不是静默忽略"
