# -*- coding: utf-8 -*-
"""项目数据库：文本出现位置与译文记录（ADR-0003、ADR-0004）。

每个汉化项目一个独立的 SQLite 项目库，存放在项目资产目录（work/<项目身份>/project.db，
core.util.store_dir）——项目包导出、迁移和删除都以项目为单位完整进行。

- **文本出现位置**：对白、菜单、strings 使用统一且稳定的标识（与翻译任务 key 同一
  体系：对白为引擎翻译块标识符 <bid>[:sN]，菜单字幕与 strings 走 "S:<原文>" 语义，
  见工单 02 的 Answer）。提取（或重新提取）后由 pipeline 调用 sync_occurrences 同步：
  标识在再次提取后保持稳定；源文本或结构指纹变化后，该出现位置的原当前译文自动
  降为**迁移建议**（用户确认后才可重新采用），绝不静默丢弃，也绝不自动恢复。
- **译文记录**：绑定出现位置，带来源（model/human/migration/import）、人工确认状态、
  产生时的源文本与结构指纹快照。相同英文原文的不同出现位置各自持有译文记录。
- **人工译文保护**（ADR-0003）：用户编辑或确认过的译文（confirmed）不可被翻译模型、
  重新提取或导入覆盖；自动过程的新结果只进入**候选译文**，采用必须经用户动作。
- **项目任务**：tasks 表登记当前项目会改核心数据的后台任务（提取、翻译、迁移、
  检查、应用），由 core.coordinator 项目任务协调器经同一连接入口读写——同一项目
  同一时间至多一个此类任务；翻译任务按记录事务逐批提交，不再整份覆盖派生镜像文件。

写路径只有本模块（工单 10 收缩步骤）：译文记录不再有整份镜像文件，运行时
不读写 work/<项目身份>/translations.json；历史数据只经 core.migration 导入
（import_currents 只补空位，供旧目录与过渡期目录的迁移使用）。
"""
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager

from . import util

_SCHEMA_VERSION = 1

# 译文记录状态与来源（CONTEXT.md：译文记录/候选译文/迁移建议）
STATUS_CURRENT = "current"
STATUS_CANDIDATE = "candidate"
STATUS_SUGGESTION = "suggestion"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS occurrences (
    occurrence_id         TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL,            -- 'say' | 'string'
    source_text           TEXT NOT NULL,            -- tl 锚点原文（ipatch 补丁不改此值）
    trans_source          TEXT,                     -- 翻译源文本（补丁后；未打补丁与源文本相同）
    who                   TEXT NOT NULL DEFAULT '',
    source_file           TEXT NOT NULL DEFAULT '',
    fingerprint           TEXT NOT NULL,
    active                INTEGER NOT NULL DEFAULT 1,  -- 是否出现在最近一次提取中
    retranslate_requested INTEGER NOT NULL DEFAULT 0,
    first_seen_at         REAL NOT NULL,
    last_seen_at          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS translations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id TEXT NOT NULL,
    text          TEXT NOT NULL,
    status        TEXT NOT NULL,                    -- 'current' | 'candidate' | 'suggestion'
    source        TEXT NOT NULL,                    -- 'model' | 'human' | 'migration' | 'import'
    confirmed     INTEGER NOT NULL DEFAULT 0,       -- 人工确认状态（人工译文保护开关）
    fingerprint   TEXT,                             -- 译文产生时的结构指纹快照
    source_text   TEXT,                             -- 译文产生时的源文本快照
    note          TEXT,                             -- 状态变化说明（如降级原因）
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trans_occ ON translations(occurrence_id, status);
-- 数据库层面的硬约束：一个出现位置同一时间至多一条当前译文
CREATE UNIQUE INDEX IF NOT EXISTS idx_trans_current
    ON translations(occurrence_id) WHERE status = 'current';
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,              -- 'extract' | 'translate' | 'check' | 'apply' | ...
    state        TEXT NOT NULL,              -- 'running' | 'done' | 'failed' | 'stopped' | 'interrupted'
    pid          INTEGER,                    -- 启动进程：崩溃后的陈旧任务按存活判定回收
    started_at   REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    finished_at  REAL,
    summary      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


def fingerprint(kind, source_file, who, source_text):
    """出现位置的结构指纹：源文本与结构上下文（类别、来源文件、说话人）的摘要。

    出现位置标识本身不含来源文件与说话人（strings 以原文为锚、translate None 块的
    标识符由作者自定），结构指纹补上这部分；再次提取时指纹变化说明出现位置的
    结构上下文已改变，旧译文不能想当然地继续适用，降为迁移建议交用户确认。
    """
    h = hashlib.sha1()
    h.update(("fp1|%s|%s|%s|%s" % (kind, source_file or "", who or "",
                                  source_text or "")).encode("utf-8"))
    return h.hexdigest()


def record_from_job(job):
    """把翻译任务（tlgen.build_jobs 的输出，含 ipatch 覆盖改写）映射为出现位置记录。

    source_text 取 tl 锚点原文（ipatch 改写时任务里 orig 保存的值），翻译源文本
    （补丁后）单独存 trans_source——补丁内容变化由 ipatch 的缓存失效机制处理，
    不影响出现位置的身份与指纹。
    """
    kind = job.get("kind") or "say"
    source_text = job.get("orig") if job.get("orig") else job["old"]
    source_file = job.get("src_file") or job.get("file", "")
    who = job.get("who", "") or ""
    return {"occurrence_id": job["key"], "kind": kind, "source_text": source_text,
            "trans_source": job["old"], "who": who, "source_file": source_file,
            "fingerprint": fingerprint(kind, source_file, who, source_text)}


class ProjectStore:
    """一个汉化项目的项目数据库。每次操作使用独立短连接，可跨线程调用。"""

    def __init__(self, project_id):
        self.project_id = str(project_id)
        self.path = os.path.join(util.store_dir(self.project_id), "project.db")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            # WAL：任务运行期间的浏览与非冲突编辑（读写并行）不被整库写锁挡住；
            # busy_timeout 兜底任务批量提交与用户编辑同时落库的瞬间争用
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.executescript(_SCHEMA)
            if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
                conn.execute("PRAGMA user_version = %d" % _SCHEMA_VERSION)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 出现位置同步 ----------

    def sync_occurrences(self, records):
        """把一次提取产生的出现位置集合同步进项目库，返回对账报告。

        - 新出现的插入（added）；标识、源文本、指纹都不变的只刷新时间戳（unchanged）；
        - 同一标识下源文本或结构指纹变化：更新记录，并把原当前译文降为迁移建议
          （changed/demoted）；
        - request_retranslation 在出现位置尚未同步时登记的占位记录（空源文本）
          就此补全真实内容，按 unchanged 计，不构成结构变化、不作降级；
        - 本次提取中消失的出现位置保留历史（active 置 0），当前译文降为迁移建议
          （removed/demoted）；重复同步已消失的不重复计数、不重复降级（幂等）。
        降为迁移建议的译文不会因出现位置重新出现而自动恢复。
        """
        now = time.time()
        with self._conn() as conn:
            existing = {r["occurrence_id"]: r for r in
                        conn.execute("SELECT * FROM occurrences").fetchall()}
            report = {"added": 0, "changed": 0, "removed": 0,
                      "unchanged": 0, "demoted": 0}
            fresh_ids = set()
            for rec in records:
                oid = rec["occurrence_id"]
                fresh_ids.add(oid)
                old = existing.get(oid)
                if old is None:
                    conn.execute(
                        "INSERT INTO occurrences (occurrence_id, kind, source_text,"
                        " trans_source, who, source_file, fingerprint, active,"
                        " first_seen_at, last_seen_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                        (oid, rec["kind"], rec["source_text"], rec.get("trans_source"),
                         rec.get("who", ""), rec.get("source_file", ""),
                         rec["fingerprint"], now, now))
                    report["added"] += 1
                    continue
                placeholder = not old["source_text"] and not old["fingerprint"]
                text_changed = old["source_text"] != rec["source_text"]
                fp_changed = old["fingerprint"] != rec["fingerprint"]
                if placeholder or text_changed or fp_changed:
                    conn.execute(
                        "UPDATE occurrences SET kind = ?, source_text = ?,"
                        " trans_source = ?, who = ?, source_file = ?, fingerprint = ?,"
                        " active = 1, last_seen_at = ? WHERE occurrence_id = ?",
                        (rec["kind"], rec["source_text"], rec.get("trans_source"),
                         rec.get("who", ""), rec.get("source_file", ""),
                         rec["fingerprint"], now, oid))
                    if placeholder:
                        report["unchanged"] += 1
                    else:
                        why = "源文本变化" if text_changed else "结构指纹变化"
                        report["changed"] += 1
                        report["demoted"] += self._demote_current(conn, oid, why)
                else:
                    if not old["active"]:
                        conn.execute(
                            "UPDATE occurrences SET active = 1 WHERE occurrence_id = ?",
                            (oid,))
                    if old["trans_source"] != rec.get("trans_source"):
                        # 翻译源文本（ipatch 补丁后）变了而锚点与指纹没变：补丁内容
                        # 变化，不构成结构变化，静默刷新即可（缓存失效由 ipatch 的
                        # 补丁指纹机制负责）
                        conn.execute(
                            "UPDATE occurrences SET trans_source = ?"
                            " WHERE occurrence_id = ?", (rec.get("trans_source"), oid))
                    report["unchanged"] += 1
            for oid, old in existing.items():
                if oid in fresh_ids or not old["active"]:
                    continue
                conn.execute("UPDATE occurrences SET active = 0 WHERE occurrence_id = ?",
                             (oid,))
                report["removed"] += 1
                report["demoted"] += self._demote_current(conn, oid, "出现位置已从提取结果中消失")
            return report

    @staticmethod
    def _current_row(conn, oid):
        """一个出现位置的当前译文行（无则 None）。"""
        return conn.execute(
            "SELECT * FROM translations WHERE occurrence_id = ? AND status = ?",
            (oid, STATUS_CURRENT)).fetchone()

    @staticmethod
    def _demote_current(conn, oid, note):
        """当前译文 -> 迁移建议。返回降级条数（无当前译文为 0）。"""
        cur = ProjectStore._current_row(conn, oid)
        if cur is None:
            return 0
        conn.execute(
            "UPDATE translations SET status = ?, confirmed = 0,"
            " note = ?, updated_at = ? WHERE id = ?",
            (STATUS_SUGGESTION, note, time.time(), cur["id"]))
        return 1

    def occurrences(self):
        """全部出现位置记录（含历史，active 标记是否在最近一次提取中）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM occurrences ORDER BY first_seen_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["active"] = bool(d["active"])
            d["retranslate_requested"] = bool(d["retranslate_requested"])
            out.append(d)
        return out

    def retranslation_requested(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT occurrence_id FROM occurrences WHERE retranslate_requested = 1"
                " ORDER BY occurrence_id").fetchall()
        return [r["occurrence_id"] for r in rows]

    def request_retranslation(self, oid):
        """请求重新翻译一个出现位置：下一次翻译会重新请求它。

        人工确认的当前译文在重译期间保持不动、继续回填；模型结果到达后只进
        候选译文（record_model_result 的保护语义）。出现位置尚未同步时先登记
        占位记录（提取同步后会补全）。
        """
        with self._conn() as conn:
            known = conn.execute("SELECT 1 FROM occurrences WHERE occurrence_id = ?",
                                 (oid,)).fetchone()
            if known is None:
                kind = "string" if str(oid).startswith("S:") else "say"
                now = time.time()
                conn.execute(
                    "INSERT INTO occurrences (occurrence_id, kind, source_text,"
                    " fingerprint, active, retranslate_requested, first_seen_at,"
                    " last_seen_at) VALUES (?, ?, '', '', 0, 1, ?, ?)",
                    (oid, kind, now, now))
            else:
                conn.execute(
                    "UPDATE occurrences SET retranslate_requested = 1"
                    " WHERE occurrence_id = ?", (oid,))

    # ---------- 译文记录 ----------

    def currents(self):
        """全部当前译文：{出现位置标识: 译文}。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT occurrence_id, text FROM translations"
                                " WHERE status = ?", (STATUS_CURRENT,)).fetchall()
        return {r["occurrence_id"]: r["text"] for r in rows}

    def current_records(self):
        """全部当前译文记录（含来源与人工确认位）：{出现位置标识: 记录}。

        编辑页状态列需要区分模型译文/人工译文（CONTEXT.md：人工译文受
        保护），currents() 只给文本不够用。
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM translations WHERE status = ?",
                                (STATUS_CURRENT,)).fetchall()
        return {r["occurrence_id"]: self._trans_dict(r) for r in rows}

    def get_current(self, oid):
        with self._conn() as conn:
            row = self._current_row(conn, oid)
        return self._trans_dict(row) if row else None

    def candidates(self, oid):
        """一个出现位置的候选译文版本（新→旧）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM translations WHERE occurrence_id = ?"
                " AND status = ? ORDER BY updated_at DESC, id DESC",
                (oid, STATUS_CANDIDATE)).fetchall()
        return [self._trans_dict(r) for r in rows]

    def suggestions(self, oid=None):
        """迁移建议：因源文本/结构指纹变化或出现位置消失而降级的旧译文。

        给 oid 时只取该出现位置的建议（编辑页打开单条时的 scoped 查询）。"""
        q = "SELECT * FROM translations WHERE status = ?"
        args = [STATUS_SUGGESTION]
        if oid is not None:
            q += " AND occurrence_id = ?"
            args.append(oid)
        with self._conn() as conn:
            rows = conn.execute(q + " ORDER BY updated_at DESC", args).fetchall()
        return [self._trans_dict(r) for r in rows]

    def review_counts(self):
        """待检查计数：{出现位置标识: {"candidates": n, "suggestions": m}}。

        候选译文（重译结果、任务期间与人工编辑冲突的结果、被替换的旧版本）与
        迁移建议都算待检查；只返回有待检查内容的出现位置。编辑页据此把行
        标为待检查，逐条打开比较（candidates()/suggestions() 按需取明细）。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT occurrence_id, status, COUNT(*) AS n FROM translations"
                " WHERE status IN (?, ?) GROUP BY occurrence_id, status",
                (STATUS_CANDIDATE, STATUS_SUGGESTION)).fetchall()
        out = {}
        for r in rows:
            d = out.setdefault(r["occurrence_id"], {"candidates": 0, "suggestions": 0})
            d["candidates" if r["status"] == STATUS_CANDIDATE else "suggestions"] = r["n"]
        return out

    def history_texts(self):
        """每个出现位置记录过的全部译文文本：{出现位置标识: {文本…}}。

        覆盖当前译文、候选译文（含被人工编辑替换的旧版本）与迁移建议——这些
        都是工具自己写进过游戏 tl 的值。无基线的首次应用前检测（ADR-0005）
        用它区分"游戏 tl 停留在上一个工具写入值（项目草稿还没应用）"与真正的
        外部译文变更。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT occurrence_id, text FROM translations").fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["occurrence_id"], set()).add(r["text"])
        return out

    @staticmethod
    def _trans_dict(row):
        d = dict(row)
        d["confirmed"] = bool(d["confirmed"])
        return d

    def _snapshot(self, conn, oid):
        row = conn.execute("SELECT source_text, fingerprint FROM occurrences"
                           " WHERE occurrence_id = ?", (oid,)).fetchone()
        return (row["source_text"], row["fingerprint"]) if row else (None, None)

    def _insert_translation(self, conn, oid, text, status, source, confirmed,
                            note=None, when=None):
        now = when or time.time()
        source_text, fp = self._snapshot(conn, oid)
        conn.execute(
            "INSERT INTO translations (occurrence_id, text, status, source,"
            " confirmed, fingerprint, source_text, note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, text, status, source, 1 if confirmed else 0, fp, source_text,
             note, now, now))

    def record_model_result(self, oid, text):
        """登记一条模型译文（翻译模型结果的唯一入口，ADR-0003）。

        - 当前译文已人工确认：新结果只进入候选译文，当前译文不动；
        - 当前译文未确认（模型/导入）：新结果成为当前译文，旧当前译文降为候选
          （版本保留）；文本相同则不产生新版本；
        - 两种情况都清除该出现位置的重译请求标记。
        单条便捷入口：与批量入口同一保护、同一冲突语义。
        """
        return self.record_model_results({oid: text})

    def record_model_results(self, mapping):
        """批量登记模型译文（一次事务）。

        返回 {"current": n, "candidate": n, "unchanged": n, "conflicts": [标识]}。
        conflicts 是人工确认的当前译文被任务结果命中的出现位置：人工译文保持
        当前不动，任务结果进入候选译文等待用户比较采用——这就是项目任务运行
        期间"冲突编辑"的落点，绝不两边覆盖、也绝不静默丢弃任何一边。"""
        counts = {"current": 0, "candidate": 0, "unchanged": 0, "conflicts": []}
        with self._conn() as conn:
            task = self._running_task(conn)
            for oid, text in mapping.items():
                outcome = self._apply_model_result(conn, oid, text, task=task)
                counts[outcome] += 1
                if outcome == "candidate":
                    counts["conflicts"].append(oid)
        return counts

    @staticmethod
    def _running_task(conn):
        """进行中的项目任务行（无则 None）。用于给并发冲突的候选译文写说明。"""
        return conn.execute(
            "SELECT * FROM tasks WHERE state = 'running'"
            " ORDER BY started_at DESC LIMIT 1").fetchone()

    def _apply_model_result(self, conn, oid, text, task=None):
        cur = self._current_row(conn, oid)
        conn.execute("UPDATE occurrences SET retranslate_requested = 0"
                     " WHERE occurrence_id = ?", (oid,))
        if cur is not None and cur["text"] == text:
            return "unchanged"
        if cur is not None and cur["confirmed"]:
            note = "人工译文保持不变，模型结果进入候选"
            if task is not None:
                note += ("（项目任务《%s》运行期间与人工编辑冲突，"
                         "请比较后采用或忽略）" % task["kind"])
            same = conn.execute(
                "SELECT id FROM translations WHERE occurrence_id = ?"
                " AND status = ? AND text = ?",
                (oid, STATUS_CANDIDATE, text)).fetchone()
            if same is None:
                self._insert_translation(conn, oid, text, STATUS_CANDIDATE, "model",
                                         False, note=note)
            elif task is not None:
                # 相同候选早已存在（此前某轮的结果）：补上本次任务的冲突说明，
                # 保证"冲突进带说明的待检查项"不依赖候选恰好是首条
                conn.execute("UPDATE translations SET note = ? WHERE id = ?",
                             (note, same["id"]))
            return "candidate"
        if cur is not None:
            conn.execute("UPDATE translations SET status = ?, note = NULL,"
                         " updated_at = ? WHERE id = ?",
                         (STATUS_CANDIDATE, time.time(), cur["id"]))
        self._insert_translation(conn, oid, text, STATUS_CURRENT, "model", False)
        return "current"

    def set_human_translation(self, oid, text):
        """登记人工译文（用户编辑/确认动作，译文记录的唯一人工入口）。

        人工译文成为当前译文并带人工确认状态；被替换的旧当前译文降为候选保留。
        """
        with self._conn() as conn:
            cur = self._current_row(conn, oid)
            if cur is not None and cur["text"] == text:
                conn.execute("UPDATE translations SET source = 'human', confirmed = 1,"
                             " updated_at = ? WHERE id = ?", (time.time(), cur["id"]))
                return
            if cur is not None:
                conn.execute("UPDATE translations SET status = ?,"
                             " updated_at = ? WHERE id = ?",
                             (STATUS_CANDIDATE, time.time(), cur["id"]))
            self._insert_translation(conn, oid, text, STATUS_CURRENT, "human", True)
            conn.execute("UPDATE occurrences SET retranslate_requested = 0"
                         " WHERE occurrence_id = ?", (oid,))

    def confirm(self, oid):
        """确认当前译文为人工认可（此后不可被自动过程覆盖）。"""
        with self._conn() as conn:
            conn.execute("UPDATE translations SET confirmed = 1"
                         " WHERE occurrence_id = ? AND status = ?", (oid, STATUS_CURRENT))

    def rewrite_current(self, oid, text):
        """机械修复当前译文文本（如错乱文本标签修正）：原位改写，不改来源、
        人工确认状态，不产生新版本。仅当当前译文存在且内容确实变化时生效，
        返回是否改写。"""
        with self._conn() as conn:
            cur = conn.execute("UPDATE translations SET text = ?, updated_at = ?"
                               " WHERE occurrence_id = ? AND status = ?"
                               " AND text != ?",
                               (text, time.time(), oid, STATUS_CURRENT, text))
            return cur.rowcount > 0

    def rewrite_human_if_current(self, oid, expect_text, new_text):
        """编辑会话内的连续人工编辑：当前译文仍是会话上一次写入的内容时，
        原位改写文本（不把每个输入停顿堆成版本历史、不污染待检查计数）。

        当前译文已被其他写入方（翻译任务等）接管、或不是人工确认译文时
        返回 False——调用方应改走 set_human_translation 标准入口，正确降级
        保留旧版本。"""
        with self._conn() as conn:
            cur = self._current_row(conn, oid)
            if (cur is None or not cur["confirmed"] or cur["source"] != "human"
                    or cur["text"] != expect_text or expect_text == new_text):
                return False
            conn.execute("UPDATE translations SET text = ?, updated_at = ?"
                         " WHERE id = ?", (new_text, time.time(), cur["id"]))
            return True

    def adopt_candidate(self, oid, translation_id):
        """用户采用一条候选译文或迁移建议（用户动作）：它成为当前译文并视为
        人工确认——迁移建议"必须经过确认，不能当作当前译文直接应用"的采用入口。"""
        with self._conn() as conn:
            cand = conn.execute(
                "SELECT id FROM translations WHERE id = ? AND occurrence_id = ?"
                " AND status IN (?, ?)",
                (translation_id, oid, STATUS_CANDIDATE, STATUS_SUGGESTION)).fetchone()
            if cand is None:
                raise ValueError("可采用的译文记录不存在：%s" % translation_id)
            conn.execute("UPDATE translations SET status = ?, updated_at = ?"
                         " WHERE occurrence_id = ? AND status = ?",
                         (STATUS_CANDIDATE, time.time(), oid, STATUS_CURRENT))
            conn.execute("UPDATE translations SET status = ?, confirmed = 1,"
                         " note = NULL, updated_at = ? WHERE id = ?",
                         (STATUS_CURRENT, time.time(), translation_id))

    def dismiss_candidate(self, oid, translation_id):
        """忽略一条候选译文/迁移建议（用户已查看并决定不采用）。

        只作用于非当前译文：待比较结果忽略即删除，当前译文与其余版本不受
        影响。这是"待检查项得到处理"的另一条出口（另一条是 adopt_candidate）。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM translations WHERE id = ? AND occurrence_id = ?"
                " AND status IN (?, ?)",
                (translation_id, oid, STATUS_CANDIDATE, STATUS_SUGGESTION)).fetchone()
            if row is None:
                raise ValueError("可忽略的译文记录不存在：%s" % translation_id)
            conn.execute("DELETE FROM translations WHERE id = ?", (translation_id,))

    # ---------- 译文记录维护（失效丢弃、清空、旧缓存导入） ----------

    def drop_currents(self, oids):
        """按出现位置丢弃当前译文（ipatch 补丁变化使旧译文失效时用）。

        未确认的当前译文降为候选保留（不静默消失）；人工确认的当前译文不受影响，
        作为受保护名单返回。返回 (已丢弃, 受保护) 两个标识列表。
        """
        dropped, protected = [], []
        with self._conn() as conn:
            for oid in oids:
                cur = self._current_row(conn, oid)
                if cur is None:
                    continue
                if cur["confirmed"]:
                    protected.append(oid)
                    continue
                conn.execute("UPDATE translations SET status = ?,"
                             " note = '补丁变化，译文作废', updated_at = ?"
                             " WHERE id = ?",
                             (STATUS_CANDIDATE, time.time(), cur["id"]))
                dropped.append(oid)
        return dropped, protected

    def clear_currents(self):
        """清空全部未确认的当前译文记录（界面上「清空译文缓存」动作的库操作）。

        人工确认的译文是人工校对成果，按 ADR-0003 保留，不随清空丢失。
        被清掉的未确认译文先整份写入项目资产目录下的恢复文件
        （cleared_currents.<时间戳>.json）——高影响操作前留恢复点、绝不
        静默丢弃（用户故事 19），需要时把内容按出现位置重新导入即可找回。
        返回保留的人工译文数。
        """
        with self._conn() as conn:
            kept = conn.execute("SELECT COUNT(*) AS n FROM translations"
                                " WHERE status = ? AND confirmed = 1",
                                (STATUS_CURRENT,)).fetchone()["n"]
            rows = conn.execute(
                "SELECT occurrence_id, text FROM translations"
                " WHERE status = ? AND confirmed = 0", (STATUS_CURRENT,)).fetchall()
            if rows:
                backup = os.path.join(
                    util.store_dir(self.project_id),
                    "cleared_currents.%d.json" % int(time.time()))
                util.write_json(backup, {r["occurrence_id"]: r["text"] for r in rows})
            conn.execute("DELETE FROM translations WHERE status = ?"
                         " AND confirmed = 0", (STATUS_CURRENT,))
        return kept

    def occurrence_count(self):
        """项目库里已同步的出现位置数（无库或库不可读为 0）。

        迁移扫描用它区分"项目库尚未建立/同步的过渡期目录"与"纯新式目录"；
        只读，不创建数据库。"""
        if not os.path.isfile(self.path):
            return 0
        try:
            conn = sqlite3.connect(self.path)
            try:
                return conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return 0        # 库损坏/被锁：按"未同步"处理，交给迁移流程（有备份兜底）

    def import_currents(self, mapping, source="import"):
        """旧版整份缓存（translations.json）一次性导入：只补空位，绝不覆盖。

        仅接受最近一次提取中仍存在、且既无当前译文也无迁移建议的出现位置——
        已降级为迁移建议的译文不因镜像文件里的旧值复活（重新采用必须经用户
        确认）；不在提取结果中的出现位置和旧缓存里的死数据一样跳过。
        source 记录导入来源：旧版镜像文件的例行导入用 import，旧数据迁移
        （core.migration）用 migration。重复调用幂等。返回 {"imported": n, "skipped": m}。
        """
        imported = skipped = 0
        with self._conn() as conn:
            for oid, text in (mapping or {}).items():
                if not isinstance(text, str) or not text:
                    skipped += 1
                    continue
                occ = conn.execute(
                    "SELECT active FROM occurrences WHERE occurrence_id = ?",
                    (oid,)).fetchone()
                has_record = conn.execute(
                    "SELECT 1 FROM translations WHERE occurrence_id = ?"
                    " AND status IN (?, ?)",
                    (oid, STATUS_CURRENT, STATUS_SUGGESTION)).fetchone()
                if occ is None or not occ["active"] or has_record is not None:
                    skipped += 1
                    continue
                self._insert_translation(conn, oid, text, "current", source, False)
                imported += 1
        return {"imported": imported, "skipped": skipped}

    def record_migration_suggestions(self, mapping, note="旧数据迁移：出现位置结构已变化，确认后可采用"):
        """把迁移数据中无法安全自动继承的旧译文登记为迁移建议（ADR-0001）。

        只登记出现位置已存在的条目（迁移建议必须绑定出现位置）；同文本建议
        已存在的不重复登记。采用经 adopt_candidate（用户动作）。返回登记条数。
        """
        n = 0
        with self._conn() as conn:
            for oid, text in (mapping or {}).items():
                if not isinstance(text, str) or not text:
                    continue
                known = conn.execute(
                    "SELECT 1 FROM occurrences WHERE occurrence_id = ?",
                    (oid,)).fetchone()
                if known is None:
                    continue
                dup = conn.execute(
                    "SELECT 1 FROM translations WHERE occurrence_id = ?"
                    " AND status = ? AND text = ?",
                    (oid, STATUS_SUGGESTION, text)).fetchone()
                if dup is not None:
                    continue
                self._insert_translation(conn, oid, text, STATUS_SUGGESTION,
                                         "migration", False, note=note)
                n += 1
        return n

    # ---------- 汇总对账 ----------

    def stats(self):
        """项目库对账统计：出现位置、当前译文、人工确认、候选与迁移建议数量。"""
        with self._conn() as conn:
            occ = conn.execute("SELECT COUNT(*) AS n FROM occurrences").fetchone()["n"]
            cur = conn.execute("SELECT COUNT(*) AS n FROM translations"
                               " WHERE status = ?", (STATUS_CURRENT,)).fetchone()["n"]
            confirmed = conn.execute("SELECT COUNT(*) AS n FROM translations"
                                     " WHERE status = ? AND confirmed = 1",
                                     (STATUS_CURRENT,)).fetchone()["n"]
            cand = conn.execute("SELECT COUNT(*) AS n FROM translations"
                                " WHERE status = ?", (STATUS_CANDIDATE,)).fetchone()["n"]
            sug = conn.execute("SELECT COUNT(*) AS n FROM translations"
                               " WHERE status = ?", (STATUS_SUGGESTION,)).fetchone()["n"]
        return {"occurrences": occ, "currents": cur, "confirmed": confirmed,
                "candidates": cand, "suggestions": sug}
