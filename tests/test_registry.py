# -*- coding: utf-8 -*-
"""工单 03 验收测试：项目注册库与稳定项目身份。

注册库是应用级 SQLite（汉化项目 = 稳定 UUID + 游戏安装关联），项目资产目录按
项目身份存放。这里的断言全部落在对外行为上：身份与目录名/绝对路径无关、
同名游戏不同路径互不串用、目录移动后可重新定位、不存在按目录名建数据目录的旁路。
"""
import json
import os

import pytest

from core import registry, util


@pytest.fixture
def reg(tmp_path):
    """每个测试一个独立的注册库（数据库文件落在该测试的临时目录）。"""
    r = registry.Registry(os.path.join(tmp_path, "registry.db"))
    try:
        yield r
    finally:
        r.close()


# ---------- 身份：与目录名、绝对路径无关 ----------

def test_project_id_is_uuid_unrelated_to_dir_name(reg, tmp_path):
    p = reg.create_project("My Game", os.path.join(tmp_path, "packA", "My Game"))
    assert p["name"] == "My Game"
    # 身份是 UUID：不含目录名，重复创建得到不同身份
    assert p["id"] and "-" in p["id"] and "My" not in p["id"] and "Game" not in p["id"]
    p2 = reg.create_project("My Game", os.path.join(tmp_path, "packB", "My Game"))
    assert p2["id"] != p["id"]


def test_find_by_path_is_case_and_separator_insensitive(reg, tmp_path):
    base = os.path.join(tmp_path, "pack", "My Game")
    reg.create_project("My Game", base)
    for probe in (base.lower(), base.upper(),
                  os.path.join(tmp_path, "pack", "My Game", "")):
        found = reg.find_by_path(probe)
        assert found is not None, probe
        assert found["name"] == "My Game"


def test_registry_persists_across_reopen(tmp_path):
    db = os.path.join(tmp_path, "registry.db")
    base = os.path.join(tmp_path, "game1")
    r1 = registry.Registry(db)
    try:
        p = r1.create_project("G1", base)
    finally:
        r1.close()
    r2 = registry.Registry(db)
    try:
        assert r2.get(p["id"])["name"] == "G1"
        assert r2.find_by_path(base)["id"] == p["id"]
    finally:
        r2.close()


def test_default_db_follows_app_dir(tmp_path, monkeypatch):
    """注册库默认位置随 app_dir 走（e2e 回归经它把全部持久化重定向进临时目录）。"""
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    r = registry.Registry()
    try:
        assert r.path == os.path.join(str(tmp_path), "registry.db")
        r.create_project("G", os.path.join(str(tmp_path), "g"))
    finally:
        r.close()
    assert os.path.isfile(os.path.join(str(tmp_path), "registry.db"))


# ---------- 同名游戏、不同路径：完全隔离 ----------

def test_same_name_games_at_different_paths_are_isolated(reg, tmp_path, monkeypatch):
    """同名目录的两个安装 = 两个项目：身份、资产目录互不相干（串数据为 0）。"""
    # 资产目录随 app_dir 重定向进临时目录（reg fixture 只重定向注册库文件）
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    a = os.path.join(tmp_path, "packA", "My Game")
    b = os.path.join(tmp_path, "packB", "My Game")
    pa = reg.create_project("My Game", a)
    pb = reg.create_project("My Game", b)
    assert pa["id"] != pb["id"]
    assert reg.find_by_path(a)["id"] == pa["id"]
    assert reg.find_by_path(b)["id"] == pb["id"]

    # 项目资产按身份存放：分别写入提取结果/译文，另一边必须完全看不到
    store_a = util.store_dir(pa["id"])
    store_b = util.store_dir(pb["id"])
    assert store_a != store_b
    assert os.path.basename(store_a) == pa["id"]  # 资产目录以身份命名，不再用目录名
    with open(os.path.join(store_a, "translations.json"), "w", encoding="utf-8") as f:
        json.dump({"k": "A 的译文"}, f)
    assert not os.path.isfile(os.path.join(store_b, "translations.json"))
    assert "A 的译文" not in os.listdir(store_b)


def test_store_dir_never_derives_from_game_dir_name(tmp_path, monkeypatch):
    """资产目录键 = 汉化项目的稳定身份；同名不同路径得到不同目录（旧 work_dir 语义已移除）。"""
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    ka = util.store_dir("11111111-1111-1111-1111-111111111111")
    kb = util.store_dir("22222222-2222-2222-2222-222222222222")
    assert ka == os.path.join(str(tmp_path), "work", "11111111-1111-1111-1111-111111111111")
    assert kb != ka
    assert not hasattr(util, "work_dir")  # 按游戏目录名建目录的入口已不存在


# ---------- 重新定位（目录改名/移动） ----------

def test_relocate_repoints_project_and_keeps_assets(reg, tmp_path, monkeypatch):
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))  # 资产目录进临时目录
    old = os.path.join(tmp_path, "old place", "My Game")
    os.makedirs(old)
    p = reg.create_project("My Game", old)
    store = util.store_dir(p["id"])
    with open(os.path.join(store, "glossary.json"), "w", encoding="utf-8") as f:
        f.write("[]")

    new = os.path.join(tmp_path, "new place", "My Game")
    os.rename(os.path.dirname(old), os.path.dirname(new))  # 整个父目录一起挪走

    # 目录已移动：旧项目出现在"当前安装丢失"名单里，等待用户确认重新定位；
    # 注册库只认登记的路径，磁盘上是否还存在由 stale_projects 判定
    stale = reg.stale_projects()
    assert [s["id"] for s in stale] == [p["id"]]

    reg.relocate(p["id"], new)
    assert reg.find_by_path(old) is None
    moved = reg.find_by_path(new)
    assert moved is not None and moved["id"] == p["id"]
    assert moved["path"].lower() == new.lower()
    # 资产跟着身份走：原数据目录原封不动继续可用
    assert os.path.isfile(os.path.join(store, "glossary.json"))
    # 历史安装保留为非当前，可追溯
    history = reg.installs(p["id"])
    cur = [i for i in history if i["is_current"]]
    prev = [i for i in history if not i["is_current"]]
    assert len(cur) == 1 and os.path.normcase(cur[0]["path"]) == os.path.normcase(new)
    assert len(prev) == 1 and os.path.normcase(prev[0]["path"]) == os.path.normcase(old)


def test_relocate_back_to_previous_install(reg, tmp_path):
    a = os.path.join(tmp_path, "a", "G")
    b = os.path.join(tmp_path, "b", "G")
    os.makedirs(a)
    os.makedirs(b)
    p = reg.create_project("G", a)
    reg.relocate(p["id"], b)
    reg.relocate(p["id"], a)  # 移回历史安装：只是重新指向，不产生重复记录
    assert reg.find_by_path(a)["id"] == p["id"]
    assert len(reg.installs(p["id"])) == 2


def test_relocate_rejects_path_registered_to_another_project(reg, tmp_path):
    pa = reg.create_project("A", os.path.join(tmp_path, "a", "G"))
    pb = reg.create_project("B", os.path.join(tmp_path, "b", "G"))
    with pytest.raises(ValueError):
        reg.relocate(pa["id"], pb["path"])
    # 失败的定位不改变任何状态
    assert reg.find_by_path(pb["path"])["id"] == pb["id"]


def test_stale_projects_only_lists_missing_directories(reg, tmp_path):
    alive = os.path.join(tmp_path, "alive", "G")
    gone = os.path.join(tmp_path, "gone", "G")
    os.makedirs(alive)
    os.makedirs(gone)
    pa = reg.create_project("A", alive)
    pg = reg.create_project("G", gone)
    os.rmdir(gone)
    assert [s["id"] for s in reg.stale_projects()] == [pg["id"]]
    assert pa["id"] not in [s["id"] for s in reg.stale_projects()]


# ---------- 登记/打开的守门 ----------

def test_create_project_rejects_already_registered_path(reg, tmp_path):
    base = os.path.join(tmp_path, "G")
    reg.create_project("G", base)
    with pytest.raises(ValueError):
        reg.create_project("G again", base)
    # 失败的登记不留下半写状态
    assert len(reg.projects()) == 1


def test_duplicate_registration_backstop_when_find_misses(reg, tmp_path, monkeypatch):
    """find 与 insert 之间被其它连接抢先登记同一路径：唯一索引兜底并转为明确报错。"""
    base = os.path.join(tmp_path, "G")
    reg.create_project("G", base)
    with monkeypatch.context() as m:
        m.setattr(reg, "find_by_path", lambda p: None)  # 模拟并发窗口里的"查不到"
        with pytest.raises(ValueError):
            reg.create_project("G again", base)
    # 唯一约束保证了同一路径仍只有一个当前安装；库可继续正常使用
    assert len(reg.projects()) == 1
    assert len(reg.installs(reg.projects()[0]["id"])) == 1
    reg.create_project("G2", os.path.join(tmp_path, "G2"))
    assert len(reg.projects()) == 2


def test_ensure_project_reuses_and_touches(tmp_path):
    r = registry.Registry(os.path.join(tmp_path, "registry.db"))
    try:
        base = os.path.join(tmp_path, "G")
        p1 = registry.ensure_project(base, name="G", reg=r)
        t0 = p1["last_opened_at"]
        p2 = registry.ensure_project(base, name="G", reg=r)
        assert p2["id"] == p1["id"]
        assert p2["last_opened_at"] >= (t0 or 0)
        assert len(r.projects()) == 1
    finally:
        r.close()


def test_ensure_project_creates_once_and_relocates_after_move(tmp_path):
    r = registry.Registry(os.path.join(tmp_path, "registry.db"))
    try:
        old = os.path.join(tmp_path, "v1", "G")
        os.makedirs(old)
        p = registry.ensure_project(old, name="G", reg=r)
        os.rename(os.path.dirname(old), os.path.join(tmp_path, "v2"))
        new = os.path.join(tmp_path, "v2", "G")
        # 用户确认后重新定位；之后 ensure 走新路径拿到的仍是同一项目
        r.relocate(p["id"], new)
        again = registry.ensure_project(new, name="G", reg=r)
        assert again["id"] == p["id"]
    finally:
        r.close()


# ---------- 游戏安装的版本记录 ----------

def test_install_version_recorded_and_updated_on_relocate(reg, tmp_path):
    base = os.path.join(tmp_path, "G")
    p = reg.create_project("G", base, version="v1.0")
    assert p["version"] == "v1.0"
    new = os.path.join(tmp_path, "G2")
    reg.relocate(p["id"], new, version="v2.0")
    assert reg.find_by_path(new)["version"] == "v2.0"
