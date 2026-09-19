# 合成补丁：机制 5 —— renpy.exports.say 猴子补丁 + 整句精确映射字典
# （条目互为子串时只能精确查表），另含补丁自有的输入提示词与默认值
# （只存在于补丁文件里，由工具收进 strings 表翻译）。
init 999 python:
    replacements = {
        "Chloe?": "Sis?",
        "I missed you, Kate.": "I missed you, Mom.",
    }

    def _patched_say(who, what, *args, **kwargs):
        if what in replacements:
            what = replacements[what]
        renpy.exports.say(who, what, *args, **kwargs)

    renpy.exports.say = _patched_say

    def _ask_relation():
        v = renpy.input("(default is Sister).")
        return v.strip() or "Sister"
