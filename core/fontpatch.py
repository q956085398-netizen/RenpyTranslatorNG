# -*- coding: utf-8 -*-
"""字体全覆盖 + 设置内语言切换入口 + 角色名替换（均不破坏原文件，可还原）。"""
import os
import re
import shutil

from .util import unesc_rpy

# ---------------------------------------------------------------- 字体

FONT_OVERRIDE_TEMPLATE = '''\
# RenpyTranslatorNG 字体覆盖：切换到本语言时生效（由翻译工具生成）
translate {lang} python:
    gui.text_font = "fonts/{name}"
    gui.name_text_font = "fonts/{name}"
    gui.interface_text_font = "fonts/{name}"
    gui.button_text_font = "fonts/{name}"
    gui.choice_button_text_font = "fonts/{name}"
    gui.glyph_font = "fonts/{name}"
    gui.input_font = "fonts/{name}"
    gui.system_font = "fonts/{name}"
    style.default.font = "fonts/{name}"
'''

_FONT_REF = re.compile(r'["\']([^"\']+?\.(?:ttf|otf|ttc|otc))["\']', re.IGNORECASE)
_BACKUP_DIR = "fonts_ng_backup"


def _referenced_fonts(gamedir):
    """扫描 game 下所有 rpy 里引用的字体文件，返回相对 game 的路径集合。"""
    refs = set()
    for root, _dirs, files in os.walk(gamedir):
        if _BACKUP_DIR in root:
            continue
        for f in files:
            if not f.endswith((".rpy", ".rpym")):
                continue
            try:
                with open(os.path.join(root, f), "r", encoding="utf-8", errors="replace") as fh:
                    for m in _FONT_REF.finditer(fh.read()):
                        refs.add(m.group(1).replace("\\\\", "/").replace("\\", "/"))
            except OSError:
                pass
    found = []
    for ref in refs:
        p = os.path.join(gamedir, *ref.split("/"))
        if os.path.isfile(p):
            found.append(ref)
        else:
            alt = os.path.join(gamedir, os.path.basename(ref))
            if os.path.isfile(alt):
                found.append(os.path.basename(ref))
    return found


def restore_fonts(game_base, log=print):
    """从备份还原被物理替换的游戏字体。"""
    gamedir = os.path.join(game_base, "game")
    bak = os.path.join(gamedir, _BACKUP_DIR)
    if not os.path.isdir(bak):
        log("没有找到字体备份（无需还原）")
        return
    n = 0
    for root, _dirs, files in os.walk(bak):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), bak)
            dst = os.path.join(gamedir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(root, f), dst)
            n += 1
    if n:
        shutil.rmtree(bak, ignore_errors=True)
    log("已还原 %d 个游戏字体文件" % n)


def apply_font(game_base, language, font_path, log=print):
    if not font_path or not os.path.isfile(font_path):
        raise RuntimeError("字体文件不存在: %s" % font_path)
    gamedir = os.path.join(game_base, "game")
    fonts_dir = os.path.join(gamedir, "fonts")
    os.makedirs(fonts_dir, exist_ok=True)
    name = os.path.basename(font_path)
    dst = os.path.join(fonts_dir, name)
    if os.path.abspath(font_path) != os.path.abspath(dst):
        shutil.copy2(font_path, dst)

    # 1) tl 样式覆盖（切回英文时自动用回原字体）
    tl_dir = os.path.join(gamedir, "tl", language)
    os.makedirs(tl_dir, exist_ok=True)
    rpy = os.path.join(tl_dir, "zz_ng_font.rpy")
    _drop(rpy + "c")
    with open(rpy, "w", encoding="utf-8") as f:
        f.write(FONT_OVERRIDE_TEMPLATE.format(lang=language, name=name))

    # 2) 物理替换游戏自带字体（备份到 fonts_ng_backup，用于彻底杜绝方块）
    with open(dst, "rb") as f:
        cjk = f.read()
    bak = os.path.join(gamedir, _BACKUP_DIR)
    n = 0
    for ref in _referenced_fonts(gamedir):
        target = os.path.join(gamedir, *ref.split("/"))
        keep = os.path.normcase(target) == os.path.normcase(dst)
        if keep:
            continue
        rel = ref.replace("/", os.sep)
        bpath = os.path.join(bak, rel)
        if not os.path.isfile(bpath):
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            shutil.copy2(target, bpath)
        with open(target, "wb") as f:
            f.write(cjk)
        n += 1
    log("字体覆盖完成：样式已写入 tl/%s/zz_ng_font.rpy；物理替换 %d 个游戏字体（备份在 game/%s，可随时还原）"
        % (language, n, _BACKUP_DIR))


def _drop(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- 语言入口（设置界面内）

LANG_ENTRY_TEMPLATE = '''\
# RenpyTranslatorNG 语言切换入口（由翻译工具生成，可删除本文件还原）
# 原理：拦截 show_screen('preferences') 重定向到增强版设置界面，
# 在原设置内容下方追加语言选择栏（主菜单与游戏内设置均可使用）。
init python early hide:
    global _ng_old_show_screen
    _ng_old_show_screen = renpy.show_screen

    def _ng_show_screen(_screen_name, *_args, **kwargs):
        if _screen_name == 'preferences':
            _screen_name = 'ng_preferences'
        return _ng_old_show_screen(_screen_name, *_args, **kwargs)

    renpy.show_screen = _ng_show_screen

screen ng_preferences():
    tag menu
    use preferences
    vbox:
        align (0.99, 0.99)
        frame:
            background Frame("#000000b0", 10, 10)
            padding (14, 10)
            vbox:
                spacing 6
                label _("Language")
                textbutton "English" action Language(None)
                textbutton "{lang_name}" action Language("{lang}")

# 语言记忆：Ren'Py 启动/读档时 _init_language() 以 config.language 优先于玩家偏好
# （renpy/common/00start.rpy：language = env or config.language or _preferences.language）。
# 部分游戏（如 AnotherChance 的 ipatch 补丁）define config.language 为假语言名，
# 会导致每次启动、读档都回退到该默认语言，玩家选择的译文语言无法保持。
# 这里覆盖 _init_language：只要玩家在设置里选过语言（_preferences.language 非空）
# 就以玩家选择为准；未选过（None）时维持游戏原默认行为。
init 1010 python:
    _ng_orig_init_language = _init_language

    def _init_language():
        pref = renpy.game.preferences.language
        if pref is not None and pref != config.language:
            renpy.change_language(pref)
        else:
            _ng_orig_init_language()
'''


def apply_lang_entry(game_base, language, lang_name=None, log=print):
    gamedir = os.path.join(game_base, "game")
    rpy = os.path.join(gamedir, "zz_ng_language.rpy")
    _drop(rpy + "c")
    name = lang_name or {"chinese": "中文"}.get(language, language)
    with open(rpy, "w", encoding="utf-8") as f:
        f.write(LANG_ENTRY_TEMPLATE.format(lang=language, lang_name=name))
    log("语言入口已写入：设置界面底部（English / %s）" % name)


# ---------------------------------------------------------------- 显示层文本映射（人名统一 + 自定义输入助手）

# 通用角色名词典：无法从人物关系表得到译名时，按词翻译（全部词都认识才翻）
_COMMON_NAME_WORDS = {
    "guard": "卫兵", "guards": "卫兵", "soldier": "士兵", "captain": "队长",
    "general": "将军", "knight": "骑士", "sir": "爵士", "lord": "领主", "lady": "夫人",
    "king": "国王", "queen": "王后", "prince": "王子", "princess": "公主",
    "maid": "女仆", "servant": "仆人", "butler": "管家", "cook": "厨师",
    "merchant": "商人", "trader": "商人", "shopkeeper": "店主", "innkeeper": "旅店老板",
    "tavernkeep": "酒馆老板", "bartender": "酒保", "patron": "客人", "guest": "客人",
    "farmer": "农夫", "hunter": "猎人", "fisherman": "渔夫", "blacksmith": "铁匠",
    "doctor": "医生", "nurse": "护士", "teacher": "老师", "priest": "神父",
    "nun": "修女", "monk": "僧侣", "bishop": "主教", "pope": "教皇",
    "narrator": "旁白", "narration": "旁白", "nobody": "无名氏", "voice": "声音",
    "man": "男人", "woman": "女人", "girl": "女孩", "boy": "男孩", "guy": "家伙",
    "old": "老", "young": "小", "male": "男", "female": "女", "mysterious": "神秘",
    "stranger": "陌生人", "traveler": "旅人", "beggar": "乞丐", "thief": "小偷",
    "bandit": "强盗", "pirate": "海盗", "assassin": "刺客", "spy": "间谍",
    "student": "学生", "professor": "教授", "master": "主人", "mistress": "女主人",
    "boss": "老板", "worker": "工人", "guard1": "卫兵1", "guard2": "卫兵2",
    "crowd": "人群", "everyone": "众人", "all": "所有", "unknown": "未知",
}

_DEFINE_CHAR = re.compile(
    r"^\s*define\s+([A-Za-z_]\w*)\s*=\s*(?:\$?\s*)?Character\s*\(\s*[\"']((?:[^\"'\\]|\\.)*)[\"']")

# Item("Lingerie", ...) 构造调用的首个字符串参数（物品显示名）
_ITEM_DEF = re.compile(r'\bItem\(\s*["\']((?:[^"\'\\]|\\.)*)["\']')


def _tl_string_map(game_base, language):
    """读 tl/<lang> 全部 strings 词条 old->new，供物品名等静态数据取译名。"""
    tl_dir = os.path.join(game_base, "game", "tl", language)
    out = {}
    if not os.path.isdir(tl_dir):
        return out
    for root, _dirs, files in os.walk(tl_dir):
        for f in files:
            if not f.endswith(".rpy"):
                continue
            try:
                src = open(os.path.join(root, f), "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in re.finditer(r'^\s*old "((?:[^"\\]|\\.)*)"\s*\n\s*new "((?:[^"\\]|\\.)*)"',
                                 src, re.M):
                try:
                    out.setdefault(unesc_rpy(m.group(1)), unesc_rpy(m.group(2)))
                except Exception:
                    pass
    return out


def char_defines(game_base):
    """扫描 game 下所有 rpy，解析 define <var> = Character("显示名")。
    返回 {变量名: 显示名}。这是作者写的真实人名，比 AI 推测可靠。"""
    gamedir = os.path.join(game_base, "game")
    out = {}
    for root, _dirs, files in os.walk(gamedir):
        if _BACKUP_DIR in root or os.sep + "tl" + os.sep in root + os.sep:
            continue
        for f in files:
            if not f.endswith((".rpy", ".rpym")):
                continue
            try:
                with open(os.path.join(root, f), "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        if ln.lstrip().startswith("#"):
                            continue
                        m = _DEFINE_CHAR.match(ln)
                        if m:
                            out[m.group(1)] = m.group(2)
            except OSError:
                pass
    return out


def _names_compat(a, b):
    """两个人名是否指同一角色（忽略大小写）。
    只认可靠的相似：完全相等，或其中一方是单词且为另一方首词的前缀
    （Thorn/Thorne、Mel/Melina、Duvessa/Duvessa Devereux）。
    复合头衔名（如 "Devereux Guard" = 德弗罗家的卫兵）即使共享姓氏也不算同一人。"""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = a.split(), b.split()
    # 所有格是另一个人（Jarik's Hooligan = 雅里克的手下），不算同名
    if "'" in a or "'" in b:
        return False
    if len(wa) == 1:
        return wb[0].startswith(wa[0])
    if len(wb) == 1:
        return wa[0].startswith(wb[0])
    return False


def _common_translate(name):
    """用通用词典按词翻译角色名；所有词都能翻译才翻（数字保留）。"""
    words = name.split()
    if not words:
        return ""
    out = []
    for w in words:
        if w.isdigit():
            out.append(w)
            continue
        t = _COMMON_NAME_WORDS.get(w.lower()) or _COMMON_NAME_WORDS.get(w.lower().strip("s"))
        if not t:
            return ""
        out.append(t)
    return "".join(out)

# 内置关系词显示默认值（英文词 -> 中文）：插件表为空时的兜底，也是工具
# 「③ 人物关系」页“填入常用关系词”按钮的来源。长幼无信息时按年长处理
# （姐姐/哥哥），具体游戏的长幼由关系词表或玩家当场输入的中文决定。
COMMON_RELATION_WORDS = {
    "sister": u"姐姐", "brother": u"哥哥", "mother": u"妈妈", "mom": u"妈妈",
    "mommy": u"妈妈", "mum": u"妈妈", "mummy": u"妈妈", "mama": u"妈妈",
    "father": u"爸爸", "dad": u"爸爸", "daddy": u"爸爸", "papa": u"爸爸",
    "aunt": u"阿姨", "auntie": u"阿姨", "uncle": u"叔叔",
    "cousin": u"表妹", "daughter": u"女儿", "son": u"儿子", "wife": u"妻子",
    "husband": u"丈夫", "roommate": u"室友", "housemate": u"室友",
    "landlady": u"房东太太", "landlord": u"房东", "tenant": u"租客",
    "friend": u"朋友", "girlfriend": u"女友", "boyfriend": u"男友",
    "partner": u"伴侣", "fiance": u"未婚夫", "fiancee": u"未婚妻",
    "teacher": u"老师", "mentor": u"导师", "student": u"学生", "boss": u"老板",
    "colleague": u"同事", "coworker": u"同事", "neighbour": u"邻居",
    "neighbor": u"邻居", "niece": u"侄女", "nephew": u"侄子",
    "father-in-law": u"岳父", "mother-in-law": u"岳母",
}

TEXT_HELPERS_TEMPLATE = r'''# RenpyTranslatorNG 显示层文本映射（由翻译工具生成，删除本文件即还原）
# 1) 人名统一：游戏中出现的角色英文名在中文模式下统一显示为固定中文译名
#    （对话框名字、正文提及、菜单、标题全覆盖，且切换回英文立即还原）
# 2) 关系词回显：台词里的关系词多是变量插值出来的（[relation1] → sister），
#    译文管不到；这里在显示层按关系词表替换：复数加「们」（[relation1]s → 姐姐们）、
#    所有格加「的」（[relation1]'s → 姐姐的），紧贴中文也照样命中
#    （"你是我的brother" → "你是我的弟弟"）。
# 3) 自定义输入：游戏要求输入英文关系词（如 sister）时可直接输入中文，
#    自动转成英文给游戏逻辑；台词回显用玩家输入的中文原词，
#    玩家输入英文时优先按本游戏关系词表、其次按提问语境（older/younger）判断长幼。
#    同一个英文词被两处提问（如 relation1 和 relation3 都是 sister）答得不一样时，
#    这份回答分不清该用在哪句台词上，一律退回本游戏关系词表。
# 4) 映射表按本文件的关系词表 + 玩家记忆（persistent）现算，且不被存档/回滚带走：
#    旧存档里存着的老映射不会盖住新表——改完关系词表，读旧档也立刻是新称呼。
init 999 python:
    import re
    _NG_LANG = "__NG_LANG__"

    _NG_NAMES = __NG_NAMES__

    # 亲属称谓短语兜底：部分游戏（如 AnotherChance 的 ipatch）按说话人把亲属占位符
    # 渲染成 your mom / your mother 等短语；含所有格 's 形式，避免 "你妈's" 漏译。
    _NG_NAMES.update({
        'your mom': u'你妈', "your mom's": u'你妈的',
        'your mother': u'你的母亲', "your mother's": u'你的母亲的',
        'your dad': u'你爸', "your dad's": u'你爸的',
        'your father': u'你的父亲', "your father's": u'你的父亲的',
    })

    # 本游戏关系词表（工具「③ 人物关系」页的“关系词显示表”）：英文词 -> 中文显示。
    # 比内置默认权威（brother 在本游戏是弟弟而不是哥哥），但玩家在游戏里直接
    # 输入的中文优先级更高——那是玩家当场做的选择。
    _NG_WORDS = __NG_WORDS__

    _NG_NORM = {
        u"姐姐": "sister", u"妹妹": "sister", u"姐妹": "sister",
        u"哥哥": "brother", u"弟弟": "brother", u"兄弟": "brother",
        u"妈妈": "mother", u"母亲": "mother", u"爸爸": "father", u"父亲": "father",
        u"阿姨": "aunt", u"叔叔": "uncle", u"表妹": "cousin", u"表姐": "cousin",
        u"女儿": "daughter", u"儿子": "son", u"妻子": "wife", u"丈夫": "husband",
        u"室友": "roommate", u"房东太太": "landlady", u"房东": "landlord",
        u"朋友": "friend", u"老师": "teacher", u"老板": "boss",
    }
    _NG_SHOW = __NG_SHOW__
    # 关系词表的中文也能反向转回英文：玩家照着表输入中文时游戏逻辑照样正确
    for _ng_k, _ng_v in _NG_WORDS.items():
        if _ng_k and _ng_v and _ng_v not in _NG_NORM:
            _NG_NORM[_ng_v] = _ng_k

    # 玩家输入的记忆：
    #   ng_input_show  英文词 -> {提问: 玩家当场写的中文}（老格式是 英文词 -> 中文）
    #   ng_input_guess 英文词 -> 按提问语境推断的中文
    # 关系词表改过之后留下的旧记忆必须整体作废：它们是按旧表算出来的（旧版还把
    # 默认值也存了进来，brother=哥哥），留着会一直盖住新设定。指纹变就清空。
    _NG_WORDS_SIG = repr(sorted((_ng_k, _ng_v) for _ng_k, _ng_v in _NG_WORDS.items()
                                if _ng_k and _ng_v))
    if getattr(persistent, "ng_words_sig", None) != _NG_WORDS_SIG:
        persistent.ng_input_show = {}
        persistent.ng_input_guess = {}
        persistent.ng_words_sig = _NG_WORDS_SIG
    if not isinstance(getattr(persistent, "ng_input_show", None), dict):
        persistent.ng_input_show = {}
    if not isinstance(getattr(persistent, "ng_input_guess", None), dict):
        persistent.ng_input_guess = {}
    for _ng_k in list(persistent.ng_input_show):
        _ng_v = persistent.ng_input_show.get(_ng_k)
        if isinstance(_ng_v, str):
            # 老格式没有提问信息。等于内置默认值的当"回声"丢掉——它可能只是默认值
            # 被存了下来，留着会把本游戏关系词表挤掉；有信息的选择（老弟）照原样迁移。
            if _ng_v and _ng_v == _NG_SHOW.get(_ng_k):
                del persistent.ng_input_show[_ng_k]
            else:
                persistent.ng_input_show[_ng_k] = {u"": _ng_v}
    for _ng_k in list(persistent.ng_input_guess):
        _ng_v = persistent.ng_input_guess.get(_ng_k)
        if _ng_v and (_ng_v == _NG_SHOW.get(_ng_k) or _ng_v == _NG_WORDS.get(_ng_k)):
            del persistent.ng_input_guess[_ng_k]
    # 非 ASCII 输入（中文/日文…）都按"玩家自己给的词"处理，直接用他的原词显示
    _NG_NONASCII = re.compile(u"[^\\x00-\\x7f]")

    def _ng_choice(en):
        """玩家对这个英文词明确写过的中文：各处回答一致才采用。

        同一个英文词可能对应游戏里两处不同的关系——本游戏 relation1 问「苏菲是你的…」、
        relation3 问「格蕾丝是苏菲的…」，两处的英文都是 sister。而显示层只看到句子里
        插值出来的英文词，分不清某句台词属于哪一处。回答互相矛盾时一律作废、退回
        关系词表（作者配置）：否则"最后一次输入"会通吃所有同词台词——给苏菲填了妹妹、
        格蕾丝那处填了姐姐，结果连"你把手机拿给妹妹们看"也变成"姐姐们"。
        """
        d = (persistent.ng_input_show or {}).get(en)
        if not d:
            return None
        if isinstance(d, str):
            return d
        vals = set(v for v in d.values() if v)
        return vals.pop() if len(vals) == 1 else None

    def _ng_cn_for(en, prompt):
        pl = (prompt or "").lower()
        younger = ("younger" in pl) or ("little" in pl)
        older = ("older" in pl) or ("elder" in pl)
        en_l = en.lower()
        if en_l == "sister" and (younger or older):
            return (u"妹妹" if younger else u"姐姐")
        if en_l == "brother" and (younger or older):
            return (u"弟弟" if younger else u"哥哥")
        return _NG_WORDS.get(en_l) or _NG_SHOW.get(en_l, en)

    _ng_old_input = renpy.input

    def _ng_input(*args, **kwargs):
        v = _ng_old_input(*args, **kwargs)
        try:
            s = (v or "").strip()
            if not s:
                return v
            prompt = args[0] if args else kwargs.get("prompt", "")
            if _NG_NONASCII.search(s):
                # 玩家直接给了中文关系词：显示就用他的原词（长幼由他决定），
                # 游戏逻辑收到对应英文；表里没有的中文输入原样交给游戏。
                # 按提问记下来：同一个英文词被问了两次而答得不一样时，
                # _ng_choice 不会采用（分不清哪句台词属于哪一处）。
                en = _NG_NORM.get(s)
                if en:
                    key = en.lower()
                    d = persistent.ng_input_show.get(key)
                    if not isinstance(d, dict):
                        d = {u"": d} if d else {}
                        persistent.ng_input_show[key] = d
                    d[(prompt or u"").strip()] = s
                    _ng_rebuild()
                    return en
                return v
            low = s.lower()
            if low in _NG_SHOW or low in _NG_WORDS:
                # 英文输入：长幼只能按提问语境猜，且猜的结果不盖关系词表
                persistent.ng_input_guess[low] = _ng_cn_for(low, prompt)
                _ng_rebuild()
            return s
        except Exception:
            return v

    renpy.input = _ng_input

    # 预编译替换层：全部词条合并成一条正则 + 一张小写键字典，init 时构建一次。
    # config.replace_text 每次文本渲染都会被调用，绝不能在函数里逐条跑正则
    # （词条多时每次调用可达毫秒级，界面一刷就吃掉一整帧）；
    # 合并后单次调用只做一次正则扫描，开销与词条数量基本无关。
    #
    # 运行期状态（映射表 + 正则）放进普通 Python 模块，不放 store：store 变量会被
    # 写进存档、也会随回滚还原。读旧档时带回来的映射是"按当时的关系词表算出来的"，
    # 会在 init 之后盖掉 init 按当前表算出的结果——症状就是关系词表改过之后，读旧档
    # 或回滚一下，台词里又是旧称呼（sister 又变回姐姐）。模块对象既不进存档也不参与
    # 回滚，读档后用的仍是 init 时按当前表建好的那份。
    import sys
    import types

    def _ng_rt():
        rt = sys.modules.get("_ng_text_runtime")
        if rt is None:
            rt = types.ModuleType("_ng_text_runtime")
            rt.map = {}
            rt.re = None
            sys.modules["_ng_text_runtime"] = rt
        return rt

    def _ng_rebuild():
        m = {}
        taken = set(k.lower() for k in _NG_NAMES)
        for k, v in _NG_SHOW.items():
            if k and v and k.lower() not in taken:
                m[k.lower()] = v
        for k, v in _NG_NAMES.items():
            if k and v and k.lower() != v.lower():
                m[k.lower()] = v
        # 后写入覆盖前面：语境推断 < 本游戏关系词表 < 玩家这次输入的中文
        for k, v in (getattr(persistent, "ng_input_guess", None) or {}).items():
            if k and v:
                m[k.lower()] = v
        for k, v in _NG_WORDS.items():
            if k and v:
                m[k.lower()] = v
        for k in (persistent.ng_input_show or {}):
            cn = _ng_choice(k)
            if isinstance(k, str) and cn:
                m[k.lower()] = cn
        rt = _ng_rt()
        rt.map = m
        rt.re = None
        if not m:
            return
        # 词边界不能用 \b：汉字在正则里也算 \w，"你是我的brother" 的 \b 失配会让
        # 整句英文词漏掉。改用 ASCII 前后视断言，紧贴中文/标点也能命中。
        # 尾部的 's / s' / s 一并吃掉，交给 _ng_sub 渲染成「的 / 们」。
        alts = sorted(m, key=len, reverse=True)
        rt.re = re.compile(
            u"(?i)(?<![A-Za-z0-9_])(?P<w>" + u"|".join(re.escape(a) for a in alts)
            + u")(?P<suf>'s|s'|s)?(?![A-Za-z0-9_])")

    def _ng_sub(mo):
        cn = _ng_rt().map.get(mo.group("w").lower())
        if not cn:
            return mo.group(0)
        suf = mo.group("suf") or ""
        if suf == "'s":
            if not cn.endswith(u"的"):
                cn += u"的"
        elif suf == "s'":
            if not cn.endswith(u"的"):
                cn += u"们的"
        elif suf == "s":
            if not cn.endswith(u"们"):
                cn += u"们"
        return cn

    def _ng_replace_text(s):
        try:
            if renpy.game.preferences.language != _NG_LANG:
                return s
            rx = _ng_rt().re
            if rx is not None and isinstance(s, str) and s:
                return rx.sub(_ng_sub, s)
            return s
        except Exception:
            return s

    config.replace_text = _ng_replace_text
    _ng_rebuild()
'''


_WORD_EN = re.compile(r"^[A-Za-z][A-Za-z' -]*$")


def relation_words_map(words):
    """关系词显示表 -> {小写英文: 中文显示}。

    接受 [{"en": "brother", "cn": "弟弟"}]（工具「③ 人物关系」页存的格式）
    或 {"brother": "弟弟"}。只收英文词条并规范成小写：这张表是在显示层做
    英文→中文替换的，"弟弟" 这种中文词条没有可命中的英文原文。
    """
    if isinstance(words, dict):
        items = list(words.items())
    else:
        items = [(w.get("en", ""), w.get("cn", ""))
                 for w in (words or []) if isinstance(w, dict)]
    out = {}
    for en, cn in items:
        en, cn = (en or "").strip(), (cn or "").strip()
        if en and cn and cn.lower() != en.lower() and _WORD_EN.match(en):
            out.setdefault(en.lower(), cn)
    return out


def apply_text_helpers(game_base, language, relations, log=print, dump=None, words=None):
    """统一写 zz_ng_text.rpy：人名显示映射 + 关系词显示表 + 自定义输入助手。

    人名映射的键必须是游戏里实际显示的名字才会在对话框生效：
    - 优先用 define xx = Character("Name") 解析出的真实人名（可靠）；
    - 译名来自人物关系表，但仅当关系表的人名与真实人名指向同一角色时才采用，
      防止 AI 推测错位（如把 Branislava 的译名安到 Shai 头上）；
    - 关系表覆盖不到的（Guard 1、Tavernkeep 等龙套）用内置词典翻译；
    - 仍不确定的名字保持英文，宁缺毋滥。

    words 是关系词显示表（英文词 -> 中文），用于游戏里靠 renpy.input 让玩家
    自填关系词的场景：词是运行期插值出来的，译文管不到，只能在显示层替换。
    """
    gamedir = os.path.join(game_base, "game")
    defines = char_defines(game_base)

    # 收集 dump 里实际出现过说话人（变量代号 + 字符串字面量名）
    whos = set()
    if dump:
        for e in dump.values():
            for n in e.get("nodes", []):
                w = n.get("who")
                if not w:
                    continue
                w = w.strip()
                whos.add(w.strip('"').strip() if w.startswith('"') and w.endswith('"') else w)

    mapping = {}
    # 1) 关系表里 AI 推测的完整人名（可能出现在正文叙述中，如 "Duvessa Devereux"）
    rel_entries = []
    for r in relations or []:
        en = (r.get("name") or "").strip()
        cn = (r.get("name_cn") or "").strip()
        if en and cn and en.lower() != cn.lower():
            rel_entries.append((en, cn))

    # 2) 真实显示名 -> 中文
    for who in sorted(whos | set(defines)):
        real = defines.get(who, who)
        if not real or real == "?":
            continue
        cn = ""
        for en, c in rel_entries:
            if _names_compat(en, real):
                cn = c
                break
        if not cn:
            cn = _common_translate(real)
        if cn and cn.lower() != real.lower():
            mapping.setdefault(real, cn)

    # 3) 关系表完整人名也保留（正文里提到全名时替换；与真实名兼容才保留，避免错位译名污染）
    real_names = list(mapping)
    for en, cn in rel_entries:
        if en in mapping:
            continue
        if any(_names_compat(en, r) for r in real_names):
            mapping.setdefault(en, cn)

    # 4) 物品名（Item("Lingerie", ...)）：运行期拼进句子的场景
    #    （"你给过她：[items]" 的插值发生在 replace_text 之前之后均可兜住），
    #    译名从 tl strings 表取，表里没有的保持英文
    try:
        tl_map = _tl_string_map(game_base, language)
        if tl_map:
            for root, _dirs, files in os.walk(gamedir):
                if _BACKUP_DIR in root or os.sep + "tl" + os.sep in root + os.sep:
                    continue
                for f in files:
                    if not f.endswith((".rpy", ".rpym")):
                        continue
                    try:
                        src = open(os.path.join(root, f), "r",
                                   encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    for m in _ITEM_DEF.finditer(src):
                        try:
                            name = unesc_rpy(m.group(1))
                        except Exception:
                            continue
                        cn = tl_map.get(name)
                        if cn and cn != name:
                            mapping.setdefault(name, cn)
    except Exception:
        pass

    rpy = os.path.join(gamedir, "zz_ng_text.rpy")
    _drop(rpy + "c")
    wmap = relation_words_map(words)
    content = (TEXT_HELPERS_TEMPLATE.replace("__NG_LANG__", language)
               .replace("__NG_NAMES__", repr(mapping))
               .replace("__NG_SHOW__", repr(COMMON_RELATION_WORDS))
               .replace("__NG_WORDS__", repr(wmap)))
    # 生成时自检：init python 块必须能通过 Python 编译，防止模板转义类 bug 流入游戏
    block = []
    inside = False
    for ln in content.split("\n"):
        if ln.startswith("init 999 python:"):
            inside = True
            continue
        if inside:
            if ln and not ln.startswith(" "):
                break
            if ln.strip():
                block.append(ln[4:] if ln.startswith("    ") else ln)
    compile("\n".join(block), "zz_ng_text.rpy", "exec")
    if "\x08" in content:
        raise RuntimeError("模板中混入控制字符，请检查 fontpatch 模板转义")
    with open(rpy, "w", encoding="utf-8") as f:
        f.write(content)
    # 清理旧版文件
    for legacy in ("zz_ng_names.rpy", "zz_ng_input.rpy"):
        _drop(os.path.join(gamedir, legacy))
        _drop(os.path.join(gamedir, legacy) + "c")
    if mapping:
        log("文本映射已写入：%d 个人名（如 %s）%s + 关系词输入助手"
            % (len(mapping), "，".join("%s→%s" % kv for kv in list(mapping.items())[:4]),
               _words_note(wmap)))
    else:
        log("文本映射已写入：关系词输入助手%s（人物关系表未填中文名，人名映射为空）"
            % _words_note(wmap))


def _words_note(wmap):
    if not wmap:
        return ""
    return "、%d 个关系词（%s）" % (len(wmap), "，".join("%s→%s" % kv for kv in list(wmap.items())[:4]))
