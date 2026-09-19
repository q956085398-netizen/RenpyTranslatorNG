# 合成补丁：机制 1 —— config.say_menu_text_filter 替换链（对白与菜单项都生效）。
init 999 python:
    def rep(text):
        text = text.replace("Diana", "your mom")
        text = text.replace("Landlady day", "Mom day")
        return text
    config.say_menu_text_filter = rep
