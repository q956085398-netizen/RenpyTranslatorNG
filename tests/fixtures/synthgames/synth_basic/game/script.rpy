# 合成回归游戏 synth_basic（原创内容，无第三方版权材料）。
# 覆盖工单 02 要求的语法形态：普通对白与菜单、同一原文的不同分支、同一 label
# 中的多个菜单节点、动态变量对白、init python 文本、多行字符串、复杂表达式
# （转义引号/[[ 转义/%%/{color} 标签）、标签与插值变量、字符串字面量说话人、
# 多主角与玩家自填关系词（renpy.input + [relation1] 回显）。
define e = Character("Eileen")
define m = Character("Marisol")
define gui.text_font = "fonts/myfont.ttf"

default player_name = "Alex"
default progress = 42
default relation1 = "sister"

label start:
    show bg vale
    e "Welcome to the Synth Vale."
    e "You are [player_name], aren't you?"
    "The vale is quiet tonight."
    e "I'm {i}so{/i} ready for this."
    menu:
        "Ask about the vale":
            e "Tell me about this place."
        "Leave":
            jump leave
    menu:
        "Look closer" if progress > 10:
            e "It is an old shrine."
        "Leave":
            jump leave
    e "Progress: [progress]%% {color=#ccffcc}and counting{/color}."
    return

label leave:
    m "Leaving already?"
    m "The road is long, [player_name]."
    return

label rivers:
    e "The river remembers."
    jump join

label lakes:
    e "The river remembers."
    jump join

label join:
    e """This blessing spans
several quiet lines."""
    "Guard" "Halt. Who goes there?"
    e "Escaped \"quotes\" and a [[bracket] stay literal."
    return

label family:
    $ relation1 = renpy.input("What is Sofia to you?", default="sister")
    e "So you are my [relation1] now."
    m "Family is family."
    return

init python:
    title = _("The Synth Vale")
    banner = "Grand Opening Soon"
