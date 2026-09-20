# -*- coding: utf-8 -*-
"""流程编排：解包 -> 反编译 -> 提取+tl骨架 -> 关系扫描 -> 翻译回填 -> 字体/语言入口。"""
import os
import shutil
import time

from . import decompile, extract, fontpatch, ipatch, project_store, pystrings, relations, texttags, tlgen, uipatch
from . import translator as xlator
from . import unrpa
from .util import fmt_exc, read_json, store_dir, write_json

TARGET_LANG_NAME = {"chinese": "简体中文"}


def _fmt_duration(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d 小时 %d 分 %d 秒" % (h, m, s)
    if m:
        return "%d 分 %d 秒" % (m, s)
    return "%d 秒" % s


def _expand_translations(jobs, translations, dropped=None):
    """返回 (原文文本 -> 译文, 任务 key -> 译文)。

    文本表沿用作统计与兜底：同一原文的多个 key 只要任一已译，全部复用同一译文。
    key 表用于逐条精确回填——同一句英文在不同分支可能有各自的既有译法，
    被人工排除补丁（项目资产目录下的 ipatch_skip.json）的条目也要保住自己的译文，
    不能被同句的其它 key 覆盖。写出前顺带修复错乱的 Ren'Py 标签（如开标签写成
    {/i}）；{} 占位符数量不一致、或 [变量] 与原文对不上（大小写/名字被模型改写、
    甚至整个翻译掉——运行时 NameError）的译文直接弃用，宁可显示英文也不能崩游戏；
    传 dropped 时把弃用的条目收进去，供调用方记日志。

    ipatch 补丁覆盖过的任务：j["old"] 是补丁后的翻译源、j["orig"] 是 tl 里的
    原文锚点——映射按 orig 为键（回填按 tl 锚点查表），占位符校验对翻译源做
    （译文是补丁后文本的翻译，占位符数量必须与翻译源一致）。"""
    groups = {}
    key_map = {}
    for j in jobs:
        if not j["old"]:
            continue
        groups.setdefault(j.get("orig") or j["old"], []).append(j)
        t = translations.get(j["key"])
        if not t:
            continue
        t = xlator.fix_rpy_tags(t)
        t, bad_interp = texttags.fix_interps(j["old"], t)
        if bad_interp:
            if dropped is not None:
                dropped.append((j["key"], bad_interp, t))
            continue
        if t.count("{}") == j["old"].count("{}"):
            key_map[j["key"]] = t
    out = {}
    for text, js in groups.items():
        for j in js:
            t = key_map.get(j["key"])
            if t:
                out[text] = t
                break
    return out, key_map


class Project:
    def __init__(self, cfg, game_base, project_id):
        """project_id：注册库登记的汉化项目稳定身份（core.registry）。

        项目资产（提取结果、词汇表、译文等）一律存放在按身份派生的项目资产目录
        （work/<项目身份>/），与游戏目录名称、绝对路径无关；
        UI 与测试都必须先经注册库登记再构造本类。"""
        self.cfg = cfg
        self.game_base = game_base
        self.project_id = project_id
        self.work = store_dir(project_id)
        self.language = cfg.get("language", "chinese")
        self._ov = None  # 最近一次 _jobs 构建出的 ipatch 覆盖（无补丁为 None）
        self._store_obj = None  # 惰性创建的项目库（core.project_store）

    # ---------- 项目库（译文记录的唯一可信来源，ADR-0004） ----------
    def store(self):
        """本汉化项目的项目数据库（core.project_store，按需创建）。"""
        if self._store_obj is None:
            self._store_obj = project_store.ProjectStore(self.project_id)
        return self._store_obj

    @staticmethod
    def _occurrence_records(jobs):
        """任务清单 -> 出现位置记录（跳过无翻译源文本的条目）。"""
        return [project_store.record_from_job(j) for j in jobs if j.get("old")]

    def ensure_store(self, jobs=None, log=None):
        """项目库就绪供界面读写：同步出现位置（给了任务清单时）+ 导入旧缓存。"""
        store = self.store()
        if jobs is not None:
            store.sync_occurrences(self._occurrence_records(jobs))
        return self._import_legacy_cache(log)

    def _import_legacy_cache(self, log=None):
        """旧版整份译文缓存（translations.json）一次性导入项目库。

        只补空位、绝不覆盖（含人工确认的当前译文）；此后该文件是项目库的
        派生镜像，供 pystrings 软去重等读取方使用，不再是译文记录的写入方。
        """
        store = self.store()
        legacy = read_json(os.path.join(self.work, "translations.json"), {}) or {}
        rep = store.import_currents(legacy)
        if rep["imported"] and log:
            log("已导入旧版译文缓存 %d 条进项目库（只补空位，已有译文与人工确认的"
                "译文不受影响）" % rep["imported"])
        return store

    def _seed_translations(self, store):
        """本次翻译的种子 = 全部当前译文，去掉已请求重译的出现位置。

        请求重译的人工译文仍留在项目库里继续回填，重译结果只会进入候选译文。"""
        seed = store.currents()
        for oid in store.retranslation_requested():
            seed.pop(oid, None)
        return seed

    def _prepare_translation_run(self, log, jobs, trans_path, store):
        """翻译/重译共用的准备：作废失效的补丁缓存，刷新一次派生镜像，返回种子。

        种子直接取自项目库（断点续翻与同文去重的依据）。模型结果按记录事务
        逐批提交进项目库，任务运行期间不再整份写镜像文件；开跑前与结束后的
        两次整份刷新只是派生镜像与项目库对账——进程中断后镜像最多滞后到上次
        刷新，项目库始终是可信来源，下次导入/翻译自然修复。"""
        self._drop_stale_overlay_cache(log, jobs, trans_path, store)
        write_json(trans_path, store.currents())
        return self._seed_translations(store)

    def _drop_stale_overlay_cache(self, log, jobs, trans_path, store):
        if not self._ov:
            return
        stale = ipatch.drop_stale_cache(jobs, self._ov, trans_path, log=log)
        if not stale:
            return
        dropped, protected = store.drop_currents(stale)
        if protected:
            log("⚠ %d 条补丁覆盖的译文已人工确认，保持不变（不随补丁状态失效）"
                % len(protected))

    def _model_commit(self, store):
        """翻译任务按记录事务提交的入口：每个批次立即登记进项目库。

        返回 (commit, conflicts)：commit 由 translator 在每批结果就绪时调用
        （一次一小事务，进程中断时已提交批次保持一致）；conflicts 是与人工确认
        译文冲突的出现位置清单——人工译文保持当前不动，任务结果进入候选译文
        （带冲突说明），绝不两边覆盖，任务结束后由用户比较采用。"""
        conflicts = []

        def commit(batch):
            conflicts.extend(store.record_model_results(batch)["conflicts"])

        return commit, conflicts

    def _translate_into_store(self, log, store, jobs, seed,
                              progress=None, should_stop=None):
        """翻译/重译共用的执行段：按记录事务逐批提交，结束后汇总冲突说明。"""
        commit, conflicts = self._model_commit(store)
        target = TARGET_LANG_NAME.get(self.language, self.language)
        xlator.translate_jobs(
            jobs, self.cfg["engine"], self.load_glossary(), self.load_relations(),
            target, progress=progress, should_stop=should_stop, log=log,
            retranslate_keys=store.retranslation_requested(),
            seed=seed, commit=commit)
        self._log_conflicts(log, sorted(set(conflicts)))

    # ---------- 各步骤 ----------
    def unpack(self, log, progress=None, should_stop=None):
        return unrpa.extract_all_archives(self.game_base, progress, should_stop)

    def decompile(self, log, should_stop=None):
        summary = decompile.decompile(self.game_base, self.cfg.get("overwrite_rpyc", False), log, should_stop)
        uipatch.patch_game(self.game_base, self.work, log)
        return summary

    def extract_tl(self, log, should_stop=None):
        # 防御：清理引擎目录的反编译残留（与引擎自带 _ren.py 冲突会让游戏无法启动）
        decompile.cleanup_common(self.game_base, log)
        n, tl_dir, json_path = extract.extract(self.game_base, self.language, log=log, should_stop=should_stop)
        shutil.copy2(json_path, os.path.join(self.work, "dump.json"))
        # ipatch 台词补丁：先解析（替换对/节点改写/补丁自有文本）
        ov = ipatch.build_overlay(self.game_base, self.work, log=log, save=True)
        # 补提取：官方机制看不到的 Python 字面量（数据驱动型游戏的界面文本）；
        # 补丁自有文本（输入提示词等）一并并入补充骨架
        try:
            added = pystrings.write_skeleton(self.game_base, self.work, self.language,
                                             dump_path=json_path, log=log,
                                             extra=(ov.extra if ov else None))
            if added:
                log("补提取 Python 字符串 %d 条（写入 tl 补充骨架，与官方提取互不影响）" % added)
        except Exception:
            log("Python 字符串补提取失败（不影响主流程）:\n%s" % fmt_exc())
        return n

    def load_dump(self):
        dump = read_json(os.path.join(self.work, "dump.json"))
        if dump is None:
            dump = read_json(os.path.join(self.game_base, "game", "ng_extract.json"))
        if not dump:
            raise RuntimeError("尚未提取：请先执行 提取+生成tl")
        return dump

    def load_glossary(self):
        return read_json(os.path.join(self.work, "glossary.json"), []) or []

    def load_relations(self):
        return read_json(os.path.join(self.work, "relations.json"), []) or []

    def load_relation_words(self):
        """关系词显示表：游戏里让玩家自填关系词（renpy.input）时，
        决定这些词在台词里显示成什么中文（如 brother → 弟弟）。"""
        return read_json(os.path.join(self.work, "relation_words.json"), []) or []

    def scan_relations(self, log, should_stop=None):
        dump = self.load_dump()
        # ipatch 补丁覆盖取样文本（只动副本）：AI 按玩家实际看到的补丁后台词
        # 推断关系，"Diana" 被补丁改成 "your mom" 的游戏不再推断出错
        try:
            ov = ipatch.build_overlay(self.game_base, self.work)
            if ov:
                dump = ov.apply_dump(dump)
        except Exception:
            pass
        # 注入游戏脚本解析出的真实人名，AI 不再猜名（猜错会导致译名错位）
        try:
            known = fontpatch.char_defines(self.game_base)
        except Exception:
            known = {}
        chars = relations.draft_relations(dump, self.cfg.get("scan", {}), self.cfg["engine"],
                                          log=log, known_names=known or None,
                                          should_stop=should_stop)
        write_json(os.path.join(self.work, "relations.json"), chars)
        log("关系草表已生成：%d 个角色（请到“人物关系”页检查修正）" % len(chars))
        return chars

    def _jobs(self, log, context_override=None):
        dump = self.load_dump()
        ctx = int(self.cfg["engine"].get("context_lines", 2))
        if context_override is not None:
            ctx = min(ctx, context_override)
        # 出现位置同步始终覆盖 strings：translate_strings 只是"任务范围要不要
        # 包含 strings"的选择，不代表 strings 出现位置消失——否则关闭该选项会把
        # strings 的当前译文误降级为迁移建议
        jobs, tl_files = tlgen.build_jobs(
            self.game_base, self.language, dump, include_strings=True,
            context_lines=ctx)
        # ipatch 台词补丁：把翻译源换成补丁后文本（tl 锚点保持原文不动）
        self._ov = ipatch.build_overlay(self.game_base, self.work, log=None)
        if self._ov:
            n = ipatch.overlay_jobs(jobs, self._ov)
            if n:
                log("ipatch 补丁覆盖：%d 条翻译源已替换为补丁后文本（译文仍按原文写回 tl）" % n)
        self.store().sync_occurrences(self._occurrence_records(jobs))
        if not self.cfg.get("translate_strings", True):
            # 任务范围不含 strings：回填文件清单也与 include_strings=False 的
            # 既有语义一致（纯 strings 文件不进入回填）
            jobs = [j for j in jobs if j["kind"] != "string"]
            keep = {j["file"] for j in jobs}
            tl_dir = os.path.join(self.game_base, "game", "tl", self.language)
            tl_files = [p for p in tl_files if os.path.relpath(p, tl_dir) in keep]
        write_json(os.path.join(self.work, "jobs.json"), jobs)
        return jobs, tl_files

    def estimate(self, log):
        jobs, _ = self._jobs(log)
        store = self._import_legacy_cache(log)
        text_map, _key_map = _expand_translations(jobs, store.currents())
        chars = sum(len(j["old"]) for j in jobs)
        done = sum(1 for j in jobs if j["old"] and (j.get("orig") or j["old"]) in text_map)
        return {"lines": len(jobs), "chars": chars, "done": done, "left": len(jobs) - done,
                "tokens_est": int(chars * 0.6) + int(chars * 0.9) + len(jobs) * 120}

    def translate(self, log, progress=None, should_stop=None):
        t0 = time.monotonic()
        jobs, tl_files = self._jobs(log)
        trans_path = os.path.join(self.work, "translations.json")
        store = self._import_legacy_cache(log)
        seed = self._prepare_translation_run(log, jobs, trans_path, store)
        prior, _prior_keys = _expand_translations(jobs, seed)
        done0 = sum(1 for j in jobs if j["old"] and (j.get("orig") or j["old"]) in prior)
        if done0:
            log("断点续翻：已译 %d / %d 条，本次只翻译剩余 %d 条（已译内容不会重复请求）"
                % (done0, len(jobs), len(jobs) - done0))
        self._translate_into_store(log, store, jobs, seed,
                                   progress=progress, should_stop=should_stop)
        dropped = []
        text_map, key_map = _expand_translations(jobs, store.currents(), dropped=dropped)
        n = tlgen.fill_translations(tl_files, text_map, key_map)
        missing = sum(1 for j in jobs if j["old"]
                    and (j.get("orig") or j["old"]) not in text_map)
        log("回填 %d 条译文，剩余未译 %d 条（未译部分显示英文原文；可再次执行续翻）" % (n, missing))
        if dropped:
            log("⚠ %d 条译文含原文没有的 [变量]（运行时必崩），已拒用、这些行继续显示英文："
                % len(dropped))
            for k, bad, t in dropped[:3]:
                log("    %s 多出 %s\n        %s" % (k, bad, t[:70]))
            if len(dropped) > 3:
                log("    …其余 %d 条" % (len(dropped) - 3))
            log("    多出的变量名改对（或删掉）后重跑本步即可回填；译文记录保存在项目库中")
        log("⏱ 翻译总用时：%s" % _fmt_duration(time.monotonic() - t0))
        # 派生镜像整份刷新（任务期间不再整份覆盖；pystrings 软去重等读取方沿用）
        write_json(trans_path, store.currents())
        return n, missing

    def retranslate_missing(self, log, progress=None, should_stop=None):
        """省钱修复：只重译没有翻译成功的出现位置（项目库里没有当前译文的）。
        请求只包含未译内容，每句至多附前后各 1 句上下文；已译内容绝不重复请求，
        人工确认的译文也绝不重新请求（除非对该条发起重译请求）。"""
        jobs, tl_files = self._jobs(log, context_override=1)
        trans_path = os.path.join(self.work, "translations.json")
        store = self._import_legacy_cache(log)
        seed = self._prepare_translation_run(log, jobs, trans_path, store)
        prior, _km = _expand_translations(jobs, seed)
        missing = [j for j in jobs if j["old"] and (j.get("orig") or j["old"]) not in prior]
        if not missing:
            log("没有未译内容，无需重译。重新翻译某条已译内容：对该译文发起重译请求，"
               "结果会作为候选译文供比较；全部重翻请用「清空译文缓存」+ 第 5 步")
            return 0, 0
        log("未译 %d 条，本次只重译这些内容（已译内容不重复请求）" % len(missing))
        t0 = time.monotonic()
        self._translate_into_store(log, store, missing, seed,
                                   progress=progress, should_stop=should_stop)
        text_map, key_map = _expand_translations(jobs, store.currents())
        n = tlgen.fill_translations(tl_files, text_map, key_map)
        missing_after = sum(1 for j in jobs if j["old"]
                          and (j.get("orig") or j["old"]) not in text_map)
        log("回填 %d 条译文，剩余未译 %d 条（可再次执行本步继续修复）" % (n, missing_after))
        log("⏱ 重译总用时：%s" % _fmt_duration(time.monotonic() - t0))
        write_json(trans_path, store.currents())
        return n, missing_after

    @staticmethod
    def _log_conflicts(log, conflicts):
        """任务期间与人工确认译文冲突的结果：汇总成一条待检查说明。"""
        if conflicts:
            log("⚠ %d 条译文与人工定稿冲突（人工译文保持当前不动，本次结果已存为"
                "候选译文、带冲突说明）：可在「④ 译文修改」比较后采用或忽略"
                % len(conflicts))

    def fill_partial(self, log):
        """不调用 API：把项目库里的当前译文回填进 tl（未译行保持英文显示），
        并应用字体/语言入口，用于翻译一部分后先试玩游戏再决定是否继续。"""
        jobs, tl_files = self._jobs(log)
        store = self._import_legacy_cache(log)
        text_map, key_map = _expand_translations(jobs, store.currents())
        n = tlgen.fill_translations(tl_files, text_map, key_map)
        missing = sum(1 for j in jobs if j["old"]
                    and (j.get("orig") or j["old"]) not in text_map)
        self.apply_font(log)
        self.apply_lang_entry(log)
        log("试玩模式：已回填 %d 条；未译 %d 条在游戏中显示英文原文" % (n, missing))
        log("想继续翻译：直接再执行第 5 步，会从剩余部分接着翻，不重复已译内容")
        return n, missing

    def apply_font(self, log):
        if self.cfg.get("apply_font", True):
            fontpatch.apply_font(self.game_base, self.language, self.cfg.get("font_path", ""), log)

    def apply_lang_entry(self, log):
        if self.cfg.get("apply_lang_entry", True):
            fontpatch.apply_lang_entry(self.game_base, self.language, log=log)

    def apply_names(self, log):
        try:
            dump = self.load_dump()
        except Exception:
            dump = None  # 没有 dump 时退回纯关系表模式
        fontpatch.apply_text_helpers(self.game_base, self.language, self.load_relations(), log,
                                     dump=dump, words=self.load_relation_words())

    def repair_cache(self, log):
        """修复译文记录中错乱的 Ren'Py 标签（{/i} 当 {i} 用、{ i } 带空格等），原位改写。

        修不掉、但会让游戏在渲染时抛 Unknown text tag 的写法（未知标签名、用 ':' 传参）
        单独报出来——这类错误引擎 lint 看不出来，只能在这里拦。
        """
        store = self._import_legacy_cache()
        trans = store.currents()
        n = 0
        bad = []
        for k, v in trans.items():
            if not isinstance(v, str):
                continue
            fixed = xlator.fix_rpy_tags(v)
            if fixed != v:
                store.rewrite_current(k, fixed)
                trans[k] = fixed
                n += 1
            for why in texttags.problems(fixed):
                bad.append((k, why, fixed))
        if n:
            write_json(os.path.join(self.work, "translations.json"), trans)
        log("标签修复完成：共 %d 条译文被修正（剩余 %d 条正常）" % (n, len(trans) - n))
        if bad:
            log("⚠ 仍有 %d 条译文的文本标签会在运行时崩溃，建议用 setline 逐条修（前 %d 条）："
                % (len(bad), min(5, len(bad))))
            for k, why, v in bad[:5]:
                log("   %s: %s\n      %s" % (k, why, v[:80]))
        return n
