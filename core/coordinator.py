# -*- coding: utf-8 -*-
"""项目任务协调器：同一汉化项目同一时间只允许一个修改核心数据的项目任务。

提取、翻译、迁移、检查、应用都作为项目任务登记（CONTEXT.md：项目任务）。
登记落在项目库（project.db）的 tasks 表上（ADR-0004：项目任务属于项目状态），
因此：

- **单写入互斥**：begin 在单个 IMMEDIATE 事务里完成"检查活跃任务 + 插入任务行"，
  同一项目的多个线程、乃至两个应用实例之间都是原子互斥；任务运行期间用户仍可
  浏览并进行非冲突编辑（编辑走项目库的受保护写入口，WAL 读写并行）。
- **崩溃可恢复**：任务行带心跳；进程中断后心跳停止，"进程已死或心跳过期"的
  任务判定为 interrupted，新任务可直接接管。SQLite 事务保证中断前已提交的批次
  完整一致、未提交的批次不产生半写状态（core.translator 按记录事务逐批提交）。
- **项目间无锁**：每个项目一个项目库，其他汉化项目的任务与游戏库（注册库、
  library.json）操作使用完全独立的文件，不被运行中的项目任务阻塞。

本模块不自己开连接：所有读写都经由 ProjectStore 的连接入口（core.project_store
是项目库的唯一写路径），任务表的结构也由项目库的建库脚本统一创建。
"""
import os
import threading
import time

from . import project_store

# 心跳间隔与陈旧判定：进程崩溃后不再有心跳；无法确认进程死活时，
# 心跳超过该时长未刷新即视为已中断（真实场景远大于单次任务批次间隔）
HEARTBEAT_INTERVAL = 10.0
STALE_SECONDS = 60.0

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"
STATE_INTERRUPTED = "interrupted"

# 模块加载时刻 ≈ 本进程启动时刻：任务行的 started_at 早于它而 pid 恰好等于本进程
# 时，说明 pid 被复用、该行是上一次进程的残留，按陈旧任务回收而不是永远阻塞
_PROCESS_STARTED_AT = time.time()


def _pid_alive(pid):
    """进程是否存活。无法判断（权限不足等）返回 None，调用方回退到心跳时效。

    Windows 用 OpenProcess + WaitForSingleObject(0)：句柄打不开且错误码为
    ERROR_INVALID_PARAMETER 即进程不存在；等不到退出信号即存活。
    POSIX 用 os.kill(pid, 0)：不存在抛 ProcessLookupError，无权限抛
    PermissionError（有这个进程，只是碰不到它）。
    """
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            SYNCHRONIZE = 0x00100000
            WAIT_TIMEOUT = 0x00000102
            ERROR_INVALID_PARAMETER = 87
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not h:
                return False if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else None
            try:
                return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
            finally:
                k32.CloseHandle(h)
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    except Exception:
        return None


def _row_info(row):
    if row is None:
        return None
    return {"id": row["id"], "kind": row["kind"], "state": row["state"],
            "pid": row["pid"], "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"], "summary": row["summary"]}


class TaskBusy(RuntimeError):
    """同一汉化项目已有修改核心数据的项目任务在运行（task 为其任务信息）。"""

    def __init__(self, task):
        self.task = task or {}
        super().__init__(
            "该项目已有任务《%s》正在运行（任务 #%d），请等待完成后再开始新任务"
            % (self.task.get("kind"), self.task.get("id", -1)))


class TaskHandle:
    """一次已登记的项目任务。

    上下文管理器用法（脚本、测试、pipeline 内部串接）在退出时自动收尾：
    异常退出记 failed 并带上异常摘要，正常退出记 done。界面等长生命周期
    场景可以在任务真正结束时再显式 finish(state, summary)。
    """

    def __init__(self, coordinator, task_id, kind):
        self.coordinator = coordinator
        self.id = task_id
        self.kind = kind
        self.summary = None
        self._finished = False
        self._stop_evt = None
        self._start_heartbeat()

    def _start_heartbeat(self):
        """心跳线程：任务运行期间定期刷新 heartbeat_at（daemon，随进程退出）。"""
        evt = threading.Event()
        self._stop_evt = evt

        def beat():
            while not evt.wait(HEARTBEAT_INTERVAL):
                try:
                    self.coordinator.heartbeat(self.id)
                except Exception:
                    pass    # 心跳偶发失败不打断任务；真正中断由陈旧接管兜底

        threading.Thread(target=beat, daemon=True).start()

    def finish(self, state=STATE_DONE, summary=None):
        """收尾任务行（幂等：重复调用不再改写已收尾的状态）。"""
        if state == STATE_RUNNING:
            raise ValueError("任务收尾状态不能是 running")
        if self._finished:
            return
        self._finished = True
        if self._stop_evt is not None:
            self._stop_evt.set()
        self.coordinator.finish(self.id, state, summary if summary is not None
                                else self.summary)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.finish(STATE_FAILED, "%s: %s" % (exc_type.__name__, exc))
        else:
            self.finish()
        return False


class TaskCoordinator:
    """一个汉化项目的任务协调器（与该项目的 ProjectStore 同库）。"""

    def __init__(self, store, stale_seconds=STALE_SECONDS):
        self.store = store
        self.stale_seconds = stale_seconds

    # ---------- 查询 ----------

    def active(self):
        """当前活跃任务的信息 dict；无活跃任务返回 None。

        顺带回收陈旧任务行（进程已死或心跳过期的 running 任务标记为
        interrupted），保证崩溃后的下一次查询/登记都能自愈。"""
        with self.store._conn() as conn:
            return _row_info(self._reclaim_stale(conn, time.time()))

    def is_busy(self):
        return self.active() is not None

    def history(self, limit=20):
        """最近的任务记录（新→旧），供任务摘要/诊断展示。"""
        with self.store._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_info(r) for r in rows]

    # ---------- 登记 ----------

    def begin(self, kind):
        """登记一个即将修改核心数据的项目任务，返回 TaskHandle。

        同一项目已有活跃任务时抛 TaskBusy——调用方（界面或测试）据此拒绝
        启动，而不是让两个任务同时写核心数据。
        """
        now = time.time()
        with self.store._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")   # 检查+插入原子化：跨线程/跨进程互斥
            task = self._reclaim_stale(conn, now)
            if task is not None:
                raise TaskBusy(_row_info(task))
            cur = conn.execute(
                "INSERT INTO tasks (kind, state, pid, started_at, heartbeat_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, STATE_RUNNING, os.getpid(), now, now))
            return TaskHandle(self, cur.lastrowid, kind)

    # ---------- 任务行维护 ----------

    def heartbeat(self, task_id):
        with self.store._conn() as conn:
            conn.execute("UPDATE tasks SET heartbeat_at = ?"
                         " WHERE id = ? AND state = ?",
                         (time.time(), task_id, STATE_RUNNING))

    def finish(self, task_id, state=STATE_DONE, summary=None):
        if state == STATE_RUNNING:
            raise ValueError("任务收尾状态不能是 running")
        with self.store._conn() as conn:
            # 只收尾仍在运行的行：心跳线程与 finish 的竞态下，迟到的收尾是空操作
            conn.execute(
                "UPDATE tasks SET state = ?, summary = ?, finished_at = ?"
                " WHERE id = ? AND state = ?",
                (state, summary, time.time(), task_id, STATE_RUNNING))

    def _reclaim_stale(self, conn, now):
        """回收陈旧任务行；仍活跃时返回该任务行（含本进程其他线程的任务）。

        - 本进程登记的 running 任务：本进程还活着，视为活跃（另一线程的任务）；
        - 其他进程：进程已死 → 立即接管；存活 → 互斥；死活无法判断 →
          心跳新鲜视为活跃，心跳过期接管；
        - 被接管的任务标记 interrupted 并带上说明（崩溃前的已提交数据不受影响）。
        """
        rows = conn.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY started_at",
            (STATE_RUNNING,)).fetchall()
        stale = []
        for r in rows:
            if r["pid"] == os.getpid() and r["started_at"] >= _PROCESS_STARTED_AT:
                # 本进程登记的任务（另一线程在跑，心跳线程活着）
                return r
            alive = _pid_alive(r["pid"])
            if alive or (alive is None and now - r["heartbeat_at"] < self.stale_seconds):
                return r
            stale.append(r)
        for r in stale:
            conn.execute(
                "UPDATE tasks SET state = ?, finished_at = ?, summary = ?"
                " WHERE id = ? AND state = ?",
                (STATE_INTERRUPTED, now,
                 "进程中断（pid %s）：任务未正常收尾，已提交的数据保持一致" % r["pid"],
                 r["id"], STATE_RUNNING))
        return None
