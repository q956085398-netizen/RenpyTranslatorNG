# -*- coding: utf-8 -*-
"""pytest 入口：把仓库根目录加进 sys.path（测试文件既可独立运行也可被 pytest 收集）。
测试一律使用临时目录，不触碰真实 work/ 数据；真实游戏的私有回归
（test_ipatch 的本机样本部分）需显式设置 NG_PRIVATE_REGRESSION=1 才会运行。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
