# -*- coding: utf-8 -*-
"""调用游戏自带 Python + 内置 unrpyc 反编译 rpyc。"""
import glob
import os
import subprocess
import sys

VENDOR_UNRPYC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "unrpyc.py")

# 程序以 pythonw（无控制台）运行时，控制台子进程会被 Windows 分配新终端窗口而闪黑框
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def game_python(game_base):
    """优先用游戏自带的精简 Python（与游戏字节码版本一致）。"""
    cands = glob.glob(os.path.join(game_base, "lib", "*", "python.exe"))
    cands += glob.glob(os.path.join(game_base, "lib", "*", "python"))
    for c in cands:
        if "windows" in c.lower() or os.name == "nt":
            return c
    return cands[0] if cands else None


def cleanup_common(game_base, log=print):
    """删除 renpy/common 下的 .rpy/.rpym。该目录官方发行时只含编译文件，
    出现源文件必然是反编译产物，会与引擎自带的 *_ren.py 冲突导致游戏无法启动。"""
    common = os.path.join(game_base, "renpy", "common")
    if not os.path.isdir(common):
        return 0
    n = 0
    for root, _dirs, files in os.walk(common):
        for f in files:
            if f.endswith((".rpy", ".rpym")):
                try:
                    os.remove(os.path.join(root, f))
                    n += 1
                except OSError:
                    pass
    if n:
        log("已清理 renpy/common 中 %d 个反编译残留文件" % n)
    return n


def _interpreters(game_base):
    """候选解释器列表：游戏自带 Python 优先，系统 Python 3 兜底。"""
    targets = []
    py = game_python(game_base)
    if py:
        targets.append(py)
    if not getattr(sys, "frozen", False) and sys.executable not in targets:
        targets.append(sys.executable)
    return targets


def decompile_dir(target_dir, game_base, log=print):
    """对任意目录跑 unrpyc（不限于 game/；用于临时分析，如提取引擎字符串）。"""
    last = ""
    for interp in _interpreters(game_base):
        proc = subprocess.run([interp, "-O", VENDOR_UNRPYC, "--clobber", target_dir],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=target_dir,
                              creationflags=CREATE_NO_WINDOW)
        if proc.returncode == 0:
            return
        last = (proc.stderr or "")[-300:]
    raise RuntimeError("unrpyc 失败: %s" % last)


def decompile(game_base, overwrite=False, log=print, should_stop=None):
    """只反编译 game/ 下的 rpyc（不碰 renpy/common）。返回摘要文本。
    解释器优先用游戏自带 Python（字节码同源），失败（如老游戏自带 Py2 读不了
    f-string 语法）则回退到系统 Python 3——unrpyc 自身能解析新旧两种字节码。"""
    cleanup_common(game_base, log)
    targets = []
    py = game_python(game_base)
    if py:
        targets.append(py)
    if not getattr(sys, "frozen", False) and sys.executable not in targets:
        targets.append(sys.executable)
    last_tail = ""
    for interp in targets:
        cmd = [interp, "-O", VENDOR_UNRPYC]
        if overwrite:
            cmd.append("--clobber")
        cmd.append(os.path.join(game_base, "game"))
        log("反编译命令: %s" % " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=game_base,
                              creationflags=CREATE_NO_WINDOW)
        out = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join(out.strip().splitlines()[-12:])
        if proc.returncode == 0:
            return tail
        last_tail = tail
        log("解释器 %s 失败（退出码 %d），尝试下一个" % (interp, proc.returncode))
    raise RuntimeError("unrpyc 全部解释器失败:\n%s" % last_tail)
