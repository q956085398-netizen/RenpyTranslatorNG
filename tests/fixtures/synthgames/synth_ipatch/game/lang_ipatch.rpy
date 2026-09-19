# 合成补丁：机制 3 —— define config.language 伪语言 + translate ipatch 块
# （整句重写与 strings old/new 对，代表补丁作者的最终文本，仅作翻译源）；
# 机制 4 —— 变量改写与说话人改名。
# 块标识符 start_f1985b21 是脚本里 d "I trust Diana this time." 的引擎标识符
# （<label>_<md5(say代码+"\r\n")[:8]>，见 tests/syntheng.py 的公式）。
define config.language = "ipatch"
default Landlady = "Mother"

init 999 python:
    d.default_name = "Mom"

translate ipatch start_f1985b21:
    d "I really trust your mom this time."

translate ipatch strings:
    old "Welcome to the estate."
    new "Welcome to the manor."
