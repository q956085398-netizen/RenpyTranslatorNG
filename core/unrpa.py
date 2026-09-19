# -*- coding: utf-8 -*-
"""纯 Python 的 RPA (RPA-2.0 / RPA-3.0) 档案解包器。"""
import os
import re
import zlib


def _xor(data, key):
    k = key.to_bytes(4, "little")
    return bytes(b ^ k[i % 4] for i, b in enumerate(data))


def _parse_index(rpa_path):
    with open(rpa_path, "rb") as f:
        header = f.readline().decode("ascii", "replace").strip()
        m = re.match(r"^(RPA-[\d.]+) ([0-9a-fA-F]+)(?: ([0-9a-fA-F]+))?$", header)
        if not m:
            raise ValueError("不是受支持的 RPA 档案: %s" % rpa_path)
        version, offset, key = m.group(1), int(m.group(2), 16), m.group(3)
        f.seek(offset)
        index_bytes = f.read()
    index = {}
    raw = zlib.decompress(index_bytes)
    for enc, items in __import__("pickle").loads(raw).items():
        name = enc
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if key is not None:
            # RPA-3.0：offset/length 也用 key 异或混淆，需还原
            k = int(key, 16)
            items = [(o ^ k, l ^ k, p) for (o, l, *rest) in items
                     for p in ([rest[0]] if rest else [b""])]
        index[name.replace("\\", "/")] = items
    return version, index


def list_archive(rpa_path):
    _, index = _parse_index(rpa_path)
    return sorted(index.keys())


def extract_archive(rpa_path, out_base, skip_if_exist=True, progress=None, should_stop=None):
    """把 rpa 解到 out_base 目录，返回 (总文件数, 解出数, 跳过数)。"""
    _, index = _parse_index(rpa_path)
    total = done = skipped = 0
    with open(rpa_path, "rb") as f:
        for name, items in index.items():
            total += 1
        for name, items in index.items():
            if should_stop and should_stop():
                break
            out_path = os.path.join(out_base, *name.split("/"))
            if skip_if_exist and os.path.isfile(out_path):
                skipped += 1
            else:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                data = b""
                for entry in items:
                    # RPA-2.0 为 (offset, length)；RPA-3.0 为 (offset, length, prefix)
                    if len(entry) >= 3:
                        offset, length, prefix = entry[0], entry[1], entry[2]
                        if isinstance(prefix, str):
                            prefix = prefix.encode("utf-8", "replace")
                    else:
                        offset, length, prefix = entry[0], entry[1], b""
                    f.seek(offset)
                    block = f.read(length)
                    # 引擎 loader 仅对索引做 XOR（见 renpy/loader.py read_index），
                    # 数据块不加密；这里尝试 zlib 解压，失败则按原样写入
                    try:
                        block = zlib.decompress(block)
                    except Exception:
                        pass
                    data += prefix + block
                with open(out_path, "wb") as out:
                    out.write(data)
                done += 1
            if progress:
                progress(total, done + skipped, name)
    return total, done, skipped


def extract_all_archives(game_base, progress=None, should_stop=None):
    """解包 game 目录下所有 rpa，返回汇总字符串。"""
    game_dir = os.path.join(game_base, "game")
    rpas = [os.path.join(game_dir, f) for f in sorted(os.listdir(game_dir)) if f.lower().endswith(".rpa")] if os.path.isdir(game_dir) else []
    if not rpas:
        return "未找到 .rpa 档案（游戏可能未打包资源）"
    parts = []
    for p in rpas:
        t, d, s = extract_archive(p, game_dir, True, progress, should_stop)
        parts.append("%s: 共%d 解出%d 跳过%d" % (os.path.basename(p), t, d, s))
    return "；".join(parts)
