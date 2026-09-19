# -*- coding: utf-8 -*-
"""本地 stub 翻译服务（测试基础设施）。

OpenAI 兼容协议的本地 HTTP 替身：实现 POST /v1/chat/completions 与
GET /v1/models，翻译由测试注入的固定表 + 兜底规则给出，逐请求可断言。
只监听 127.0.0.1 随机端口——测试不产生任何外部网络请求与 API 费用，
却走完整的 requests -> 协议解析 -> 批次对齐 -> 缓存链路（最高接缝）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def default_translate(text):
    """兜底翻译：确定性包裹，保留全部 {标签} 与 [变量]（回填校验可过）。"""
    return "【%s】" % text if text else text


class StubTranslator(object):
    """一个测试用的固定翻译服务连接。

    table: {原文: 译文}；未命中走 default_translate。
    译文必须保留原文的 {} 占位符数量与 [变量] 集合，否则回填会被拒用。"""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.requests = []          # 每次请求收到的 items（断言用）
        self.request_count = 0
        self._lock = threading.Lock()

    def translate_text(self, text):
        return self.table.get(text) or default_translate(text)

    def _handle_chat(self, payload):
        messages = payload.get("messages") or []
        content = messages[-1].get("content", "") if messages else ""
        items = json.loads(content)
        with self._lock:
            self.request_count += 1
            self.requests.append(items)
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("i"):
                out.append({"i": it["i"], "t": self.translate_text(it.get("text", ""))})
        return json.dumps(out, ensure_ascii=False)

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self._reply({"id": "stub", "object": "chat.completion",
                             "choices": [{
                                 "index": 0,
                                 "message": {"role": "assistant",
                                             "content": stub._handle_chat(payload)},
                                 "finish_reason": "stop"}]})

            def do_GET(self):
                if not self.path.rstrip("/").endswith("/models"):
                    self.send_response(404)
                    self.end_headers()
                    return
                self._reply({"object": "list",
                             "data": [{"id": "stub-model", "object": "model"}]})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = "http://127.0.0.1:%d/v1" % self._server.server_address[1]
        return self

    def stop(self):
        if getattr(self, "_server", None):
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None
