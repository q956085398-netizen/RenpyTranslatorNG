# -*- coding: utf-8 -*-
"""运行时提取：向游戏注入 hook，运行游戏一次性完成
1) 对白元数据导出 (ng_extract.json)  2) 官方机制生成 tl 空翻译骨架。
"""
import glob
import json
import os
import subprocess
import time

from .util import fmt_exc

HOOK_NAME = "zz_ng_extract.rpy"

HOOK_TEMPLATE = '''\
# RenpyTranslatorNG 运行时提取钩子（翻译完成后可删除本文件）
init 999 python:
    import os, json, traceback
    _ng_gamedir = renpy.config.gamedir
    _ng_flag = os.path.join(_ng_gamedir, "ng_extract.flag")
    if os.path.isfile(_ng_flag):
        _ng_logf = None
        try:
            import io as _io
            _ng_logf = _io.open(os.path.join(_ng_gamedir, "ng_extract.log"), "w", encoding="utf-8")
            def _ng_log(m):
                _ng_logf.write(m + "\\n"); _ng_logf.flush()
            try:
                os.remove(_ng_flag)
            except Exception:
                pass
            with _io.open(os.path.join(_ng_gamedir, "ng_extract.lang"), "r", encoding="utf-8") as _f:
                _ng_lang = _f.read().strip()
            _ng_log("language = " + _ng_lang)

            translator = renpy.game.script.translator
            dump = {}
            for identifier, value in translator.default_translates.items():
                entry = {"filename": getattr(value, "filename", ""), "lineno": getattr(value, "linenumber", 0)}
                # TranslateSay 自身带 what（8.5 起也有 block 属性，须先看 what）；
                # Translate 块则遍历其 block 子节点
                if hasattr(value, "what"):
                    cand = [value]
                else:
                    cand = list(getattr(value, "block", None) or [])
                nodes = []
                for node in cand:
                    if hasattr(node, "what"):
                        nodes.append({"type": "say", "who": getattr(node, "who", "") or "", "what": node.what})
                    elif hasattr(node, "items"):
                        caps = []
                        for it in node.items:
                            try:
                                caps.append(it[0])
                            except Exception:
                                caps.append("")
                        nodes.append({"type": "menu", "captions": caps})
                if nodes:
                    entry["nodes"] = nodes
                    dump[identifier] = entry
            _ng_log("extracted identifiers: %d" % len(dump))
            with _io.open(os.path.join(_ng_gamedir, "ng_extract.json"), "w", encoding="utf-8") as _f:
                _f.write(json.dumps(dump, ensure_ascii=False))

            import renpy.translation.generation as _gen
            _ng_log("generating tl skeleton ...")
            _n = 0
            for _fn in _gen.translate_list_files():
                _gen.write_translates(_fn, _ng_lang, _gen.empty_filter)
                _n += 1
            _gen.write_strings(_ng_lang, _gen.empty_filter, 0, 299, False)
            _gen.write_strings(_ng_lang, _gen.empty_filter, 0, 299, True)
            _gen.close_tl_files()
            _ng_log("tl source files: %d" % _n)
            with _io.open(os.path.join(_ng_gamedir, "ng_extract.done"), "w", encoding="utf-8") as _f:
                _f.write("identifiers=%d" % len(dump))
            _ng_log("=== done ===")
        except Exception:
            _e = traceback.format_exc()
            try:
                if _ng_logf:
                    _ng_logf.write(_e)
                    _ng_logf.close()
                with open(os.path.join(_ng_gamedir, "ng_extract.error"), "w", encoding="utf-8") as _f:
                    _f.write(_e)
            except Exception:
                print(_e)
        renpy.quit()
'''


def find_game_exe(game_base):
    exes = [f for f in glob.glob(os.path.join(game_base, "*.exe"))
            if not f.lower().endswith(("uninstall.exe", "setup.exe"))]
    if not exes:
        return None
    # 优先选择与目录同名的 exe（游戏主程序）
    base = os.path.basename(os.path.normpath(game_base))
    for e in exes:
        if os.path.splitext(os.path.basename(e))[0] == base:
            return e
    return exes[0]


def extract(game_base, language, timeout=180, log=print, should_stop=None):
    """注入钩子并运行游戏，完成提取与 tl 骨架生成。返回 (标识符数量, tl 目录)。"""
    gamedir = os.path.join(game_base, "game")
    exe = find_game_exe(game_base)
    if not exe:
        raise RuntimeError("找不到游戏主程序 exe")
    for f in ("ng_extract.done", "ng_extract.error", "ng_extract.json"):
        p = os.path.join(gamedir, f)
        if os.path.exists(p):
            os.remove(p)
    with open(os.path.join(gamedir, "ng_extract.lang"), "w", encoding="utf-8") as f:
        f.write(language)
    hook_path = os.path.join(gamedir, HOOK_NAME)
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(HOOK_TEMPLATE)
    open(os.path.join(gamedir, "ng_extract.flag"), "w").close()

    log("启动游戏进程执行提取: %s" % exe)
    proc = subprocess.Popen([exe], cwd=game_base)
    start = time.time()
    done = os.path.join(gamedir, "ng_extract.done")
    err = os.path.join(gamedir, "ng_extract.error")
    try:
        while time.time() - start < timeout:
            if should_stop and should_stop():
                proc.kill()
                raise RuntimeError("用户停止")
            if os.path.isfile(err):
                with open(err, "r", encoding="utf-8") as f:
                    raise RuntimeError("游戏内提取失败:\n" + f.read())
            if os.path.isfile(done):
                break
            time.sleep(0.5)
        else:
            proc.kill()
            extra = ""
            for logname in ("ng_extract.log", "log.txt", "errors.txt", "traceback.txt"):
                logp = os.path.join(game_base, logname)
                if not os.path.isfile(logp):
                    logp = os.path.join(gamedir, logname)
                if os.path.isfile(logp):
                    try:
                        tail = open(logp, encoding="utf-8", errors="replace").read().splitlines()[-12:]
                        if tail:
                            extra += "\n[%s]\n%s" % (logname, "\n".join(tail))
                    except OSError:
                        pass
            raise RuntimeError("提取超时（游戏未在 %d 秒内完成，常见原因：游戏弹了错误框）。%s" % (timeout, extra))
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        # 等进程真正退出，释放对游戏文件（字体等）的占用
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        for f in ("ng_extract.flag", "ng_extract.lang"):
            p = os.path.join(gamedir, f)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    json_path = os.path.join(gamedir, "ng_extract.json")
    if not os.path.isfile(json_path):
        raise RuntimeError("提取完成但未找到 ng_extract.json")
    with open(json_path, "r", encoding="utf-8") as f:
        dump = json.load(f)
    try:
        os.remove(hook_path)
    except Exception:
        pass
    gen_common_tl(game_base, language)
    tl_dir = os.path.join(gamedir, "tl", language)
    log("提取完成：对白标识符 %d 个，tl 目录 %s" % (len(dump), tl_dir))
    return len(dump), tl_dir, json_path


def _existing_tl_olds(gamedir, language):
    """收集 tl/<语言>/ 下已有的 old 字符串（不含 common.rpy），用于去重。"""
    import re
    from .util import unesc_rpy
    olds = set()
    tl_dir = os.path.join(gamedir, "tl", language)
    if not os.path.isdir(tl_dir):
        return olds
    for root, _dirs, files in os.walk(tl_dir):
        for f in files:
            if not f.endswith(".rpy") or f == "common.rpy":
                continue
            try:
                with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                    for m in re.finditer(r'^\s*old "((?:[^"\\]|\\.)*)"', fh.read(), re.M):
                        olds.add(unesc_rpy(m.group(1)))
            except OSError:
                pass
    return olds


# 引擎官方将这些文件归为 developer/obsolete，普通游戏翻译不应包含
DEV_COMMON_FILES = ("00gltest", "00gamepad", "00console", "00director", "00updater",
                    "00performance", "00db", "00obsolete", "00layout",
                    "_compat", "_developer", "_layout")


def gen_common_tl(game_base, language, log=print):
    """生成 tl/<语言>/common.rpy（主菜单/设置/存读档等引擎 UI 字符串）。
    优先级：已存在 > tl/None/common.rpym 转换 > 临时反编译 renpy/common 扫描。"""
    import re
    import shutil
    import tempfile
    from .util import esc_rpy, unesc_rpy
    gamedir = os.path.join(game_base, "game")
    dst = os.path.join(gamedir, "tl", language, "common.rpy")
    # common.rpy 已存在则不重写（避免重复翻译），但仍走扫描以更新 layout 刷新文件
    skip_strings = os.path.isfile(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    src = os.path.join(gamedir, "tl", "None", "common.rpym")
    if os.path.isfile(src):
        game_olds = _existing_tl_olds(gamedir, language)
        out = ["translate %s strings:\n" % language]
        n = 0
        pending_old = None
        cur_src = ""
        with open(src, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.rstrip("\r\n")
                mc = re.match(r"^    # (\S+)", ln)
                if mc:
                    cur_src = mc.group(1)
                    continue
                m = re.match(r'^    old "((?:[^"\\]|\\.)*)"$', ln)
                if m:
                    pending_old = m.group(1)
                    continue
                if ln.startswith('    new "') and pending_old is not None:
                    dev = any(cur_src.startswith(d) or ("/" + d) in cur_src for d in DEV_COMMON_FILES)
                    if not dev and unesc_rpy(pending_old) not in game_olds:
                        out.append('\n    old "%s"\n    new ""\n' % pending_old)
                        n += 1
                    pending_old = None
        with open(dst, "w", encoding="utf-8") as f:
            f.write("".join(out))
        return n

    # 路线三：把 renpy/common 拷到临时目录反编译，扫描 _("...") 字符串
    common_dir = os.path.join(game_base, "renpy", "common")
    if not os.path.isdir(common_dir):
        return 0
    from . import decompile
    tmp = tempfile.mkdtemp(prefix="ng_common_")
    try:
        tmp_common = os.path.join(tmp, "common")
        shutil.copytree(common_dir, tmp_common,
                        ignore=shutil.ignore_patterns("*.rpyc", "*.rpymc"))
        # 复制 rpyc/rpymc（源文件若在原目录会被 .rpy 覆盖，这里只取编译产物）
        for root, _dirs, files in os.walk(common_dir):
            for f in files:
                if f.endswith((".rpyc", ".rpymc")):
                    rel = os.path.relpath(os.path.join(root, f), common_dir)
                    target = os.path.join(tmp_common, rel)
                    if not os.path.exists(target):
                        shutil.copy2(os.path.join(root, f), target)
        try:
            decompile.decompile_dir(tmp_common, game_base, log)
        except Exception as e:
            log("common 反编译失败（跳过部分引擎字符串）: %s" % e)
        # 支持 _() 内多个相邻字面量（隐式拼接）与单双引号
        lit = r'(?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
        pat = re.compile(r'''\b__?\s*\(\s*[uU]?\s*((?:%s)(?:\s*%s)*)\s*\)''' % (lit, lit))
        lit_re = re.compile(lit)
        # 引擎 layout 确认框常量（初始化时求值一次，运行时切换语言不会刷新）
        const_re = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*_\(\s*[uU]?(%s)\s*\)\s*$' % lit)
        layout_consts = []
        dev_files = DEV_COMMON_FILES
        seen = []
        for root, _dirs, files in os.walk(tmp_common):
            rel_root = os.path.relpath(root, tmp_common).replace("\\", "/")
            skip_dir = any(rel_root == d or rel_root.startswith(d + "/") for d in dev_files)
            for f in sorted(files):
                if skip_dir or not f.endswith((".rpy", ".rpym", ".py")):
                    continue
                if any(f.startswith(d) for d in dev_files):
                    continue
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except OSError:
                    continue
                if f == "00gui.rpy":
                    for line in content.splitlines():
                        mc = const_re.match(line)
                        if mc:
                            layout_consts.append((mc.group(1), unesc_rpy(lit_re.search(mc.group(2)).group(0)[1:-1])))
                for m in pat.finditer(content):
                    s = "".join(unesc_rpy(x[1:-1]) for x in lit_re.findall(m.group(1)))
                    if s and s not in seen:
                        seen.append(s)
        game_olds = _existing_tl_olds(gamedir, language)
        seen = [s for s in seen if s not in game_olds]
        if not skip_strings:
            blocks = ["translate %s strings:\n" % language]
            for s in seen:
                blocks.append('\n    old "%s"\n    new ""\n' % esc_rpy(s))
            with open(dst, "w", encoding="utf-8") as f:
                f.write("".join(blocks))
        _write_layout_refresh(gamedir, language, layout_consts)
        return 0 if skip_strings else len(seen)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


LAYOUT_REFRESH_TEMPLATE = '''\
# RenpyTranslatorNG 引擎确认框文本刷新（由翻译工具生成）
# layout 的确认文案在引擎初始化时只求值一次，运行时切换语言不会自动刷新，
# 这里注册官方 change_language 回调，切换语言后重新求值（切回英文也自动还原）。
init 999 python:
    def _ng_refresh_layout():
        try:
%s
        except Exception:
            pass

    config.change_language_callbacks.append(_ng_refresh_layout)
'''


def _write_layout_refresh(gamedir, language, consts):
    from .util import esc_rpy
    path = os.path.join(gamedir, "tl", language, "zz_ng_layout.rpy")
    pyc = path + "c"
    if os.path.exists(pyc):
        os.remove(pyc)
    if not consts:
        if os.path.exists(path):
            os.remove(path)
        return 0
    body = "\n".join('            layout.%s = _("%s")' % (name, esc_rpy(text)) for name, text in consts)
    with open(path, "w", encoding="utf-8") as f:
        f.write(LAYOUT_REFRESH_TEMPLATE % body)
    return len(consts)
