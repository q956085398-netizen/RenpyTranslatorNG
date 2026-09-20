# -*- coding: utf-8 -*-
"""应用级项目注册库：汉化项目的稳定身份与其游戏安装关联（ADR-0001、ADR-0004）。

每个汉化项目拥有与游戏目录名称、绝对路径无关的稳定 UUID；项目资产按该身份
存放（work/<项目身份>/）。注册库是应用级 SQLite 数据库，记录：
- 汉化项目（身份、名称、创建/最近打开时间）；
- 游戏安装关联（路径、版本）：同一汉化项目同一时间只有一个当前游戏安装，
  历史安装保留可追溯；不同位置的同名游戏是不同的游戏安装，绝不自动合并。
目录改名或移动后，由用户确认把汉化项目重新定位到新路径（relocate）。

路径只作为"当前游戏安装"的属性，不参与身份；比较时大小写与分隔符不敏感
（不解析 8.3 短路径与符号链接，同一物理目录经不同路径形式登记会被视为
不同的游戏安装，宁可分裂也不自动合并）。
"""
import os
import sqlite3
import time
import uuid

from . import util

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    created_at     REAL NOT NULL,
    last_opened_at REAL
);
CREATE TABLE IF NOT EXISTS installs (
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path          TEXT NOT NULL,
    path_key      TEXT NOT NULL,
    version       TEXT,
    is_current    INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    PRIMARY KEY (project_id, path_key)
);
CREATE INDEX IF NOT EXISTS idx_installs_path ON installs(path_key, is_current);
-- 数据库层面的硬约束：同一路径同一时间至多是一个汉化项目的当前游戏安装
-- （并发登记同一路径时由本约束兜底，绝不产生串数据的重复项目）
CREATE UNIQUE INDEX IF NOT EXISTS idx_installs_current_path
    ON installs(path_key) WHERE is_current = 1;
"""


def default_db_path():
    """注册库默认位置（应用目录下）。随 app_dir 解析，测试可整体重定向。"""
    return os.path.join(util.app_dir(), "registry.db")


def _norm(p):
    """路径比较键：分隔符与大小写不敏感（与游戏库 Library._key 同一规则）。"""
    return os.path.normcase(os.path.normpath(p))


def _display_path(p):
    """入库存储的路径：规范分隔符，保留大小写供展示。"""
    return os.path.normpath(p)


class Registry:
    """项目注册库。单线程使用；每个实例持有独立连接。"""

    def __init__(self, path=None):
        self.path = path or default_db_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- 查询 ----------

    def _attach_current(self, proj):
        if proj is None:
            return None
        cur = self.conn.execute(
            "SELECT path, version FROM installs WHERE project_id = ? AND is_current = 1",
            (proj["id"],)).fetchone()
        proj = dict(proj)
        proj["path"] = cur["path"] if cur else None
        proj["version"] = cur["version"] if cur else None
        return proj

    def get(self, project_id):
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._attach_current(row) if row else None

    def find_by_path(self, game_path):
        """按当前游戏安装路径找汉化项目；未登记返回 None。"""
        row = self.conn.execute(
            "SELECT p.* FROM projects p JOIN installs i ON i.project_id = p.id "
            "WHERE i.is_current = 1 AND i.path_key = ?", (_norm(game_path),)).fetchone()
        return self._attach_current(row) if row else None

    def projects(self):
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [self._attach_current(r) for r in rows]

    def stale_projects(self):
        """当前游戏安装目录已不存在（被移动/改名/删除）的汉化项目。"""
        return [p for p in self.projects()
                if p["path"] and not os.path.isdir(p["path"])]

    def installs(self, project_id):
        """汉化项目的全部游戏安装（含历史），按首次登记时间排序。"""
        rows = self.conn.execute(
            "SELECT path, version, is_current, first_seen_at FROM installs "
            "WHERE project_id = ? ORDER BY first_seen_at", (project_id,)).fetchall()
        return [{"path": r["path"], "version": r["version"],
                 "is_current": bool(r["is_current"]), "first_seen_at": r["first_seen_at"]}
                for r in rows]

    # ---------- 变更 ----------

    def _add_install(self, project_id, game_path, version, when):
        """登记一个游戏安装并设为当前安装（新路径插入，历史路径重新指向）。"""
        key = _norm(game_path)
        known = self.conn.execute(
            "SELECT 1 FROM installs WHERE project_id = ? AND path_key = ?",
            (project_id, key)).fetchone()
        if known is not None:
            self.conn.execute(
                "UPDATE installs SET is_current = 1, version = ? "
                "WHERE project_id = ? AND path_key = ?", (version, project_id, key))
        else:
            self.conn.execute(
                "INSERT INTO installs (project_id, path, path_key, version, is_current, first_seen_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (project_id, _display_path(game_path), key, version, when))

    def create_project(self, name, game_path, version=None):
        """登记新的汉化项目及其首个游戏安装（当前安装）。"""
        if not game_path:
            raise ValueError("游戏安装路径不能为空")
        dup = self.find_by_path(game_path)
        if dup is not None:
            raise ValueError("该游戏安装已登记为汉化项目：%s" % dup["name"])
        pid = str(uuid.uuid4())
        now = time.time()
        try:
            self.conn.execute(
                "INSERT INTO projects (id, name, created_at, last_opened_at) VALUES (?, ?, ?, ?)",
                (pid, name or os.path.basename(os.path.normpath(game_path)), now, now))
            self._add_install(pid, game_path, version, now)
            self.conn.commit()
        except sqlite3.IntegrityError:
            # find 与 insert 之间另一连接登记了同一路径：唯一索引兜底，绝不产生串项目的重复登记
            self.conn.rollback()
            other = self.find_by_path(game_path)
            raise ValueError("该游戏安装已登记为汉化项目：%s"
                             % (other["name"] if other else game_path)) from None
        return self.get(pid)

    def relocate(self, project_id, new_path, version=None):
        """用户确认目录移动/改名（或换装同项目的新版本）后，重新指向当前游戏安装。

        旧当前安装保留为历史记录；新路径若已属于其它汉化项目则拒绝（绝不自动合并）。
        """
        if not self.get(project_id):
            raise ValueError("汉化项目不存在：%s" % project_id)
        if not new_path:
            raise ValueError("新的游戏安装路径不能为空")
        other = self.find_by_path(new_path)
        if other is not None and other["id"] != project_id:
            raise ValueError("该路径已是其它汉化项目的游戏安装：%s" % other["name"])
        try:
            self.conn.execute("UPDATE installs SET is_current = 0 WHERE project_id = ?",
                              (project_id,))
            self._add_install(project_id, new_path, version, time.time())
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            raise ValueError("该路径已是其它汉化项目的游戏安装") from None
        return self.get(project_id)

    def delete_project(self, project_id):
        """删除汉化项目的登记与其全部游戏安装关联（级联）。

        项目资产目录（work/<项目身份>/）由调用方处理；用于迁移失败回滚等
        需要整体撤销登记的场景。返回是否删除了记录。
        """
        cur = self.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def touch(self, project_id):
        self.conn.execute("UPDATE projects SET last_opened_at = ? WHERE id = ?",
                          (time.time(), project_id))
        self.conn.commit()


def ensure_project(game_path, name=None, version=None, reg=None):
    """打开游戏目录对应的汉化项目：已登记则直接返回并记录打开时间，
    首次使用则登记新项目。

    非交互场景（脚本、迁移、测试）的便捷入口；界面层经
    ui.main.MainWindow._resolve_project 走同一条登记路径，并在创建/重新定位前
    插入用户确认（目录移动后的重新定位绝不自动合并）。"""
    own = reg is None
    if own:
        reg = Registry()
    try:
        proj = reg.find_by_path(game_path)
        if proj is not None:
            reg.touch(proj["id"])
            return reg.get(proj["id"])
        return reg.create_project(name, game_path, version)
    finally:
        if own:
            reg.close()
