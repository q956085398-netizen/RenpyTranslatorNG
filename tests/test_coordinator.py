# -*- coding: utf-8 -*-
"""工单 06 验收测试：单项目单写入任务协调器 + 翻译按记录事务提交。

断言全部落在对外行为上：

- 同一汉化项目同一时间只允许一个修改核心数据的项目任务（跨线程、跨进程），
  其他汉化项目与游戏库（注册库）操作不被锁住；
- 翻译运行时编辑其他记录（含与任务结果冲突的记录），两边数据都不丢失；
  冲突结果进入带说明的候选译文，任务结束后可比较采用；
- 进程中断（真实子进程强杀，模拟崩溃）后：已提交批次保持一致，未提交批次
  不产生半写状态，协调器任务可被接管，翻译可断点续跑；
- 每个批次经记录事务立即提交进项目库，不再整份覆盖派生镜像文件。

翻译服务一律是本地 stub（零网络零费用），引擎替身沿用 tests.syntheng；
app_dir 随每份测试重定向，不触碰真实 work/ 与注册库。
"""
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import coordinator, extract, pipeline, project_store, registry, tlgen, util  # noqa: E402
from tests import stubserver, syntheng  # noqa: E402

GAME_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "synthgames", "synth_basic")


# ---------- 夹具与工具 ----------

@pytest.fixture
def app(tmp_path, monkeypatch):
    """app_dir 重定向到测试临时目录：注册库与项目资产都不碰真实数据。"""
    monkeypatch.setattr(util, "app_dir", lambda: str(tmp_path))
    return tmp_path


def make_occ(oid, text="Hello.", who="e", file="script.rpy"):
    return {"occurrence_id": oid, "kind": "say", "source_text": text,
            "trans_source": text, "who": who, "source_file": file,
            "fingerprint": project_store.fingerprint("say", file, who, text)}


@pytest.fixture
def store(app):
    s = project_store.ProjectStore("11111111-1111-1111-1111-111111111111")
    s.sync_occurrences([make_occ("lbl_a")])
    return s


@pytest.fixture
def coord(store):
    return coordinator.TaskCoordinator(store)


def make_cfg(base_url):
    return {
        "language": "chinese",
        "engine": {"base_url": base_url, "api_key": "stub-key", "model": "stub-model",
                   "temperature": 0.0, "concurrency": 1, "batch_size": 2,
                   "context_lines": 2, "timeout": 600, "max_retry": 1,
                   "max_tokens": 0, "max_prompt_tokens": 0, "thinking": "auto",
                   "system_prompt": ""},
        "apply_font": False, "font_path": "", "apply_lang_entry": False,
        "translate_strings": True,
    }


def wait_until(fn, timeout=60, what="条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.02)
    raise AssertionError("等待超时：%s" % what)


def insert_running_task(store, pid, heartbeat_age=0.0):
    """直接造一条"别的进程留下的 running 任务行"（该进程写库后崩溃的状态）。"""
    now = time.time()
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO tasks (kind, state, pid, started_at, heartbeat_at)"
            " VALUES ('翻译', 'running', ?, ?, ?)",
            (pid, now - heartbeat_age, now - heartbeat_age))


def register_project(tmp_path, game, name):
    reg = registry.Registry()
    try:
        return reg.create_project(name, game)["id"]
    finally:
        reg.close()


def read_tl_all(game):
    out = []
    tl_root = os.path.join(game, "game", "tl", "chinese")
    for root, _dirs, files in os.walk(tl_root):
        for f in files:
            with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                out.append(fh.read())
    return "\n".join(out)


def batch_keys(stub_request_items):
    return [it["i"] for it in stub_request_items if isinstance(it, dict) and it.get("i")]


# ---------- 协调器：登记、互斥、收尾 ----------

def test_begin_registers_task_and_finish_records_summary(coord):
    with coord.begin("翻译"):
        active = coord.active()
        assert active is not None and active["kind"] == "翻译"
        assert active["pid"] == os.getpid()
    assert coord.active() is None
    hist = coord.history()
    assert hist[0]["state"] == "done" and hist[0]["summary"] is None


def test_second_task_on_same_project_rejected(coord):
    with coord.begin("翻译"):
        with pytest.raises(coordinator.TaskBusy) as ei:
            coord.begin("提取")
        assert "翻译" in str(ei.value)
    # 前一个任务结束后，新任务可以立即开始
    with coord.begin("提取"):
        assert coord.active()["kind"] == "提取"


def test_finish_is_idempotent_and_validates_state(coord):
    task = coord.begin("翻译")
    task.finish("stopped", "用户暂停")
    task.finish("done", "迟到的收尾不改写状态")
    hist = coord.history()
    assert hist[0]["state"] == "stopped" and hist[0]["summary"] == "用户暂停"
    with pytest.raises(ValueError):
        task.finish("running")


def test_failed_task_records_exception_summary(coord):
    try:
        with coord.begin("提取"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    hist = coord.history()
    assert hist[0]["state"] == "failed" and "boom" in hist[0]["summary"]


def test_begin_is_atomic_under_thread_race(coord):
    """多个线程同时登记：恰好一个成功，其余全部被互斥拒绝。"""
    results = []

    def race():
        try:
            coord.begin("翻译")
            results.append("ok")
        except coordinator.TaskBusy:
            results.append("busy")

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("busy") == 7


# ---------- 崩溃恢复：陈旧任务按进程死活接管 ----------

def test_dead_process_task_reclaimed_immediately(app, coord, store):
    """子进程登记任务后直接消亡（模拟崩溃）：新任务立即接管，任务行带说明。"""
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from core import util, project_store, coordinator\n"
        "util.app_dir = lambda: %r\n"
        "s = project_store.ProjectStore(%r)\n"
        "c = coordinator.TaskCoordinator(s)\n"
        "c.begin('翻译')\n"
        "print('begun', flush=True)\n" % (ROOT, str(app), store.project_id))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=60)
    assert "begun" in proc.stdout, proc.stderr
    # 子进程已消亡：其 running 任务行应被立即回收，新任务直接开始
    with coord.begin("提取"):
        assert coord.active()["kind"] == "提取"
    interrupted = [h for h in coord.history() if h["state"] == "interrupted"]
    assert len(interrupted) == 1 and interrupted[0]["kind"] == "翻译"
    assert "中断" in (interrupted[0]["summary"] or "")


def test_living_process_blocks_then_dead_process_is_reclaimed(app, coord, store):
    """活的进程持有任务 → 互斥；它一死 → 立即接管。"""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; print('up', flush=True); time.sleep(60)"],
                            stdout=subprocess.PIPE, text=True)
    try:
        proc.stdout.readline()
        insert_running_task(store, pid=proc.pid)
        with pytest.raises(coordinator.TaskBusy):
            coord.begin("提取")
    finally:
        proc.kill()
        proc.wait(timeout=30)
    with coord.begin("提取"):
        # 新任务已在跑；被接管的旧任务行标记为 interrupted
        assert coord.active()["kind"] == "提取"
        assert coord.history()[1]["state"] == "interrupted"


def test_unknowable_liveness_falls_back_to_heartbeat(coord, store, monkeypatch):
    """死活无法判断时回退心跳时效：心跳新鲜拒绝。"""
    monkeypatch.setattr(coordinator, "_pid_alive", lambda pid: None)
    coord.stale_seconds = 60.0
    insert_running_task(store, pid=424242, heartbeat_age=0.0)
    with pytest.raises(coordinator.TaskBusy):
        coord.begin("提取")


def test_stale_heartbeat_reclaims_when_liveness_unknowable(coord, store, monkeypatch):
    monkeypatch.setattr(coordinator, "_pid_alive", lambda pid: None)
    coord.stale_seconds = 0.05
    insert_running_task(store, pid=424242, heartbeat_age=5.0)
    with coord.begin("提取"):
        # 新任务已在跑；被接管的旧任务行标记为 interrupted
        assert coord.history()[1]["state"] == "interrupted"


# ---------- 其他项目与游戏库不被锁 ----------

def test_other_project_and_registry_not_blocked(app, coord):
    with coord.begin("翻译"):
        # 其他汉化项目：独立项目库，任务照常登记与结束
        sb = project_store.ProjectStore("22222222-2222-2222-2222-222222222222")
        cb = coordinator.TaskCoordinator(sb)
        with cb.begin("翻译"):
            assert cb.active()["kind"] == "翻译"
        # 游戏库（注册库是独立的应用级数据库）：登记、打开时间照常写
        game = os.path.join(str(app), "gameA")
        os.makedirs(os.path.join(game, "game"), exist_ok=True)
        reg = registry.Registry()
        try:
            reg.touch(register_project(app, game, "另一个游戏"))
        finally:
            reg.close()
        # 本项目自己仍然互斥
        with pytest.raises(coordinator.TaskBusy):
            coord.begin("提取")


# ---------- 模型结果冲突：人工译文保护 + 带说明的候选 ----------

def test_model_result_on_confirmed_current_reports_conflict(coord, store):
    store.record_model_result("lbl_a", "模型的译文。")
    store.set_human_translation("lbl_a", "人工定稿。")
    # 无任务在跑：候选照常产生（既有重译语义），人工译文不动
    store.record_model_result("lbl_a", "模型的新译文。")
    assert store.get_current("lbl_a")["text"] == "人工定稿。"
    # 任务运行期间：冲突被点名，候选带任务说明
    with coord.begin("翻译"):
        rep = store.record_model_results({"lbl_a": "更新的模型译文。"})
    assert rep["conflicts"] == ["lbl_a"] and rep["candidate"] == 1
    assert store.get_current("lbl_a")["text"] == "人工定稿。"
    cand = store.candidates("lbl_a")[0]
    assert cand["text"] == "更新的模型译文。"
    assert "冲突" in cand["note"] and "翻译" in cand["note"]


# ---------- 翻译运行时的并发编辑：两边数据都不丢失 ----------

class GatedStub(stubserver.StubTranslator):
    """可控时序的 stub：第 gate_at 个请求等到 gate 放行才回复。

    用它制造确定性的并发编辑窗口：请求 2 到达时，第 1 批已提交进项目库，
    第 2 批还在途——测试在这个窗口里完成编辑再放行。"""

    def __init__(self, table=None, gate_at=None):
        super().__init__(table)
        self.gate_at = gate_at
        self.gate = threading.Event()
        self.gate_reached = threading.Event()

    def _gate(self, n):
        if self.gate_at is not None and n >= self.gate_at:
            self.gate_reached.set()
            assert self.gate.wait(timeout=120), "gate 等待超时"


def test_concurrent_edit_during_translation_no_data_loss(app, monkeypatch):
    """翻译在跑：编辑"任务即将翻译的记录"（冲突）与"已译记录"（非冲突），
    两边都完好，回填使用合并后的项目库状态。"""
    monkeypatch.setattr(extract, "extract", syntheng.extract_for_test)
    stub = GatedStub(gate_at=2).start()
    try:
        game = os.path.join(str(app), "games", "synth_basic")
        shutil.copytree(GAME_SRC, game)
        pid = register_project(app, game, "并发编辑")
        p = pipeline.Project(make_cfg(stub.base_url), game, pid)
        p.extract_tl(log=lambda m: None)
        coord = coordinator.TaskCoordinator(p.store())
        jobs, _files = _build_jobs(p)
        # 一条人工定稿（不请求重译）——验证任务期间它纹丝不动
        fixed_key = next(k for k, j in jobs.items() if j["old"] == "The river remembers.")
        p.store().set_human_translation(fixed_key, "河水记得（人工定稿）。")

        errors = []

        def run_translate():
            try:
                with coord.begin("翻译"):
                    p.translate(log=lambda m: None)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        worker = threading.Thread(target=run_translate)
        worker.start()
        # 第 2 批请求到达（第 1 批已提交）时做并发编辑，保证窗口确定性
        wait_until(lambda: stub.gate_reached.is_set(), what="第 2 批翻译请求")
        store = p.store()
        first_batch = batch_keys(stub.requests[0])
        later_batch = batch_keys(stub.requests[1])
        edited_conflict = later_batch[0]          # 任务即将翻译：冲突位
        edited_done = first_batch[0]              # 已有模型结果：非冲突位
        store.set_human_translation(edited_conflict, "人工抢先编辑（冲突位）。")
        store.set_human_translation(edited_done, "人工改写已译位。")
        stub.gate.set()
        worker.join(timeout=120)
        assert not worker.is_alive() and not errors, errors

        # ---------- 冲突位：人工译文保持当前，任务结果进候选（带说明） ----------
        cur = store.get_current(edited_conflict)
        assert cur["text"] == "人工抢先编辑（冲突位）。" and cur["confirmed"]
        conflict_cand = next(c for c in store.candidates(edited_conflict)
                             if c["text"] != cur["text"])
        assert "冲突" in (conflict_cand["note"] or "")
        # ---------- 非冲突位：人工改写生效，被替换的模型结果保留为候选 ----------
        cur2 = store.get_current(edited_done)
        assert cur2["text"] == "人工改写已译位。" and cur2["source"] == "human"
        assert any(c["text"] != cur2["text"] for c in store.candidates(edited_done))
        # ---------- 任务期间的人工定稿纹丝不动 ----------
        assert store.get_current(fixed_key)["text"] == "河水记得（人工定稿）。"
        # ---------- 任务结束后可安全合并：冲突候选经用户采用成为当前译文 ----------
        store.adopt_candidate(edited_conflict, conflict_cand["id"])
        merged = store.get_current(edited_conflict)
        assert merged["text"] == conflict_cand["text"] and merged["confirmed"]
        # 全部出现位置最终都有当前译文（模型结果 + 人工编辑合并，一条不丢）
        st = store.stats()
        assert st["currents"] == st["occurrences"], st
        # 回填结果：tl 里各条译文各得其所（冲突位人工优先、定稿位保持）
        tl = read_tl_all(game)
        assert "人工抢先编辑（冲突位）。" in tl
        assert "人工改写已译位。" in tl
        assert "河水记得（人工定稿）。" in tl
        # 任务正常收尾
        assert coord.history()[0]["state"] == "done"
    finally:
        stub.stop()


def _build_jobs(p):
    dump = p.load_dump()
    jobs, files = tlgen.build_jobs(p.game_base, p.language, dump,
                                   include_strings=True, context_lines=2)
    return ({j["key"]: j for j in jobs if j.get("old")}, files)


# ---------- 进程中断：已提交批次一致、未提交无半写、可续跑 ----------

class HangOnceStub(stubserver.StubTranslator):
    """第 hang_at 个请求只挂起一次（制造"进程中断窗口"），此后照常回复。"""

    def __init__(self, table=None, hang_at=None):
        super().__init__(table)
        self.hang_at = hang_at
        self.hung = threading.Event()
        self._release = threading.Event()

    def _gate(self, n):
        if n == self.hang_at and not self.hung.is_set():
            self.hung.set()
            self._release.wait(timeout=600)


CHILD_TRANSLATE = '''
import sys
ROOT, TMP, GAME = %(root)r, %(tmp)r, %(game)r
sys.path.insert(0, ROOT)
from core import util, extract, pipeline, registry
util.app_dir = lambda: TMP
from tests import syntheng
extract.extract = syntheng.extract_for_test
reg = registry.Registry()
try:
    pid = reg.create_project("中断回归", GAME)["id"]
finally:
    reg.close()
p = pipeline.Project(%(cfg)r, GAME, pid)
p.extract_tl(log=lambda m: None)
from core import coordinator, project_store
coord = coordinator.TaskCoordinator(project_store.ProjectStore(pid))
with coord.begin("翻译"):
    p.translate(log=lambda m: None)
print("translate-done", flush=True)
'''


def test_interrupted_process_keeps_committed_batches(app):
    """子进程翻译到第 3 批在途时被强杀（TerminateProcess，真正的进程中断）：

    - 已提交批次（第 1、2 批）在项目库里完整一致；
    - 在途批次（第 3 批）不产生任何半写；
    - 协调器任务行可被接管（interrupted）；
    - 断点续跑补齐其余内容，已提交批次不会被重复请求。"""
    hang = HangOnceStub(hang_at=3).start()
    try:
        game = os.path.join(str(app), "games", "synth_basic")
        shutil.copytree(GAME_SRC, game)
        cfg = make_cfg(hang.base_url)
        script = CHILD_TRANSLATE % {"root": ROOT, "tmp": str(app), "game": game,
                                    "cfg": cfg}
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=ROOT)
        try:
            # 并发为 1 时批次严格串行：第 3 批请求到达 ⇒ 第 1、2 批已提交
            wait_until(lambda: hang.request_count >= 3 or proc.poll() is not None,
                       timeout=90, what="子进程翻译进入第 3 批（前两批已提交）")
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=30)
                raise AssertionError("子进程提前退出：\n%s\n%s" % (out[-1500:], err[-1500:]))
        finally:
            if proc.poll() is None:
                proc.kill()       # TerminateProcess：真正的进程中断
            proc.wait(timeout=30)
        assert proc.returncode != 0

        store = project_store.ProjectStore(_project_id_of(game))
        committed = batch_keys(hang.requests[0]) + batch_keys(hang.requests[1])
        inflight = batch_keys(hang.requests[2])
        assert committed and inflight
        currents = store.currents()
        for k in committed:
            row = store.get_current(k)
            assert row is not None and row["source"] == "model" and row["text"], k
        for k in inflight:
            assert k not in currents, "未提交批次出现半写：%s" % k
        # 协调器：中断任务可被接管
        coord = coordinator.TaskCoordinator(store)
        assert coord.active() is None
        interrupted = [h for h in coord.history() if h["state"] == "interrupted"]
        assert len(interrupted) == 1 and interrupted[0]["kind"] == "翻译"

        # 断点续跑：其余内容补齐，已提交批次不重复请求
        p = pipeline.Project(make_cfg(hang.base_url), game, _project_id_of(game))
        before = len(hang.requests)
        with coordinator.TaskCoordinator(p.store()).begin("翻译"):
            p.translate(log=lambda m: None)
        resumed_keys = set()
        for r in hang.requests[before:]:
            resumed_keys.update(batch_keys(r))
        assert not (resumed_keys & set(committed)), "已提交批次被重复请求"
        st = store.stats()
        assert st["currents"] == st["occurrences"], st
        assert [h["state"] for h in coord.history()][:2] == ["done", "interrupted"]
    finally:
        hang.stop()


def _project_id_of(game):
    reg = registry.Registry()
    try:
        return reg.find_by_path(game)["id"]
    finally:
        reg.close()
