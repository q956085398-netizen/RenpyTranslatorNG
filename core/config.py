# -*- coding: utf-8 -*-
"""应用配置的持久化。"""
import os

from .util import app_dir, read_json, write_json

CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULTS = {
    # 翻译引擎（OpenAI 兼容协议；DeepSeek / 本地 Ollama / LM Studio 均可）
    "engine": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.6,
        "presence_penalty": 0.0,  # 量化本地模型（Qwen 等）建议 1.5 防复读；API 可保持 0
        "thinking": "auto",      # 思考模式：auto=不发送参数 | off=关闭 | low/high=开启并设强度
                                 # （DeepSeek V4 系列默认开启思考且按输出计费，翻译类任务建议 off）
        "concurrency": 8,
        "batch_size": 12,        # 每次请求翻译的行数
        "context_lines": 2,      # 每行携带的前后文行数（只读参考）
        "timeout": 240,
        "max_retry": 4,
        "max_tokens": 0,         # 单次回复上限；0 = 不发送该参数（在线 API 保持原样）。
                                 # 本地模型务必设置：不设时模型会一直写到上下文窗口用满，
                                 # llama.cpp/LM Studio 直接抛 500 并连带掐断同批并发请求
        "max_prompt_tokens": 0,  # 输入预算（估算 token）；0 = 不限制。
                                 # 本地模型建议填「模型上下文长度的一半左右」，超预算的批次自动拆小
        "system_prompt": "",     # 自定义翻译 System Prompt；空 = 使用内置默认
    },
    # 人物关系扫描所用模型：same = 与翻译引擎相同；custom = 使用下方独立端点
    "scan": {
        "use": "same",           # same | custom
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "model": "",
        "sample_per_char": 40,   # 每个角色抽样台词数
    },
    "language": "chinese",       # tl 目录语言名
    "theme": "dark",             # 界面主题：dark | light（右上角按钮切换）
    "profiles": [],              # 已保存的接口方案 [{name, base_url, api_key, model, ...引擎全部参数}]
    "f95_cookie": "",            # 可选：F95Zone 登录 Cookie（评分查询用；匿名请求有每小时次数上限）
    "font_path": "C:/WINDOWS/Fonts/msyh.ttc",
    "apply_font": True,
    "apply_lang_entry": True,
    "apply_default_lang": True,
    "overwrite_rpyc": False,     # 反编译时是否覆盖已存在的 rpy
    "translate_strings": True,   # 是否连 UI 字符串一起翻译
    "auto_check_updates": True,  # 启动后是否在后台静默检查游戏库有没有新版本
}


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        saved = read_json(CONFIG_PATH, {}) or {}
        for k, v in saved.items():
            if k in DEFAULTS and isinstance(v, dict) and isinstance(DEFAULTS[k], dict):
                merged = dict(DEFAULTS[k])
                merged.update(v)
                self[k] = merged
            else:
                self[k] = v

    def save(self):
        write_json(CONFIG_PATH, dict(self))
