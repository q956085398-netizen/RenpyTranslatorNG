# 合成回归游戏 synth_ipatch（原创内容，无第三方版权材料）。
# 配套多机制 ipatch 台词补丁（工单 02）：say_menu_text_filter 替换链、
# StringTranslator 猴子补丁、translate ipatch 伪语言块、变量改写与说话人改名、
# renpy.exports.say 整句字典补丁、补丁自有输入提示词；storypatch.rpy 是
# 名字带 patch 的普通剧情文件（不得误判为补丁）。
define d = Character("Diana")
define k = Character("Kate")

label start:
    d "I trust Diana this time."
    d "I missed you, Kate."
    k "Chloe?"
    $ quest_title = "Landlady day"
    d "Welcome to the estate."
    menu:
        "Enter the house":
            d "After you."
        "Walk the grounds":
            d "Fresh air helps."
    return
