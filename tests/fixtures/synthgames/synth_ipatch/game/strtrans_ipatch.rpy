# 合成补丁：机制 2 —— renpy.translation.StringTranslator 猴子补丁替换链。
init 999 python:
    _orig_translate = renpy.translation.StringTranslator.translate

    def _patched_translate(self, s):
        s = s.replace("estate", "manor")
        return _orig_translate(self, s)

    renpy.translation.StringTranslator.translate = _patched_translate
