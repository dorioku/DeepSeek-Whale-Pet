"""
独立 API 中转服务 —— “类似 DSH 的监听”。

桌宠内置一个 OpenAI 兼容的本地中转服务：
  客户端把 DeepSeek 的 base_url 指向 http://127.0.0.1:<port>/v1，
  所有 chat/completions 请求经桌宠转发到 DeepSeek，桌宠从响应 usage 中
  按峰谷定价换算成本，并回调 GUI 弹出“本轮对话消耗”气泡 —— 不需要 DSH，
  也不需要浏览器。

支持：
  - POST /v1/chat/completions（流式 / 非流式）
  - GET  /v1/models
  - 其余 /v1/* 请求透明转发
  - GET  /healthz（自检）
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from .pricing import cost_from_usage

# OpenAI 兼容响应的 usage 结构（转换 DeepSeek 字段）
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens",
               "prompt_tokens_details", "completion_tokens_details")


def _normalize_usage(usage: dict) -> dict:
    if not isinstance(usage, dict):
        return {}
    return {k: usage.get(k) for k in _USAGE_KEYS if k in usage}


class _Handler(BaseHTTPRequestHandler):
    server: "ProxyServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    # ------------ helpers ------------
    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit: int = 32 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return b""
        return self.rfile.read(length)

    def _forward(self, path: str, body: bytes | None = None, method: str = "GET") -> dict:
        """透明转发到 DeepSeek，返回 {ok, status, body(bytes), json?}。"""
        if requests is None:
            return {"ok": False, "status": 500,
                    "body": '{"error":"requests 未安装"}'.encode("utf-8")}
        url = self.server.api_base.rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        if self.server.api_key:
            headers["Authorization"] = "Bearer " + self.server.api_key
        try:
            stream = bool(getattr(self.server, "_pending_stream", False))
            self.server._pending_stream = False
            resp = requests.request(method, url, data=body, headers=headers,
                                    timeout=self.server.timeout, stream=True)
            if path.endswith("/chat/completions") and resp.status_code == 200 and stream:
                return {"ok": True, "status": resp.status_code, "resp": resp}
            hdrs = {k: v for k, v in resp.headers.items()}
            data = resp.content
            resp.close()
            return {"ok": True, "status": resp.status_code, "headers": hdrs,
                    "body": data}
        except requests.RequestException as e:
            return {"ok": False, "status": 502,
                    "body": json.dumps({"error": {"message": str(e)}}).encode("utf-8")}

    def _relay_stream(self, resp):
        """SSE 流式转发：边转发边收集 usage（DeepSeek 最后一条 data 带 usage）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        usage = {}
        try:
            for chunk in resp.iter_lines(decode_unicode=True):
                if chunk is None:
                    continue
                line = chunk.rstrip("\n")
                self.wfile.write((line + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    if payload and self.server.on_turn:
                        try:
                            obj = json.loads(payload)
                            if isinstance(obj.get("usage"), dict):
                                usage = obj["usage"]
                        except ValueError:
                            pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            resp.close()
            self.close_connection = True
            if usage and self.server.on_turn:
                model = self.server.last_model
                info = cost_from_usage(model, usage)
                self.server.on_turn(model, _normalize_usage(usage),
                                    info["cost"], info["tokens"]["total"])

    def _relay_json(self, status: int, headers: dict, consumed: bytes):
        """非流式：转发 JSON 并统计 usage。"""
        self.send_response(status)
        for k, v in (headers or {}).items():
            if k.lower() in ("content-length", "transfer-encoding", "connection",
                             "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(consumed)))
        self.end_headers()
        self.wfile.write(consumed)
        if status == 200 and self.server.on_turn:
            try:
                obj = json.loads(consumed.decode("utf-8", "replace"))
            except ValueError:
                return
            usage = obj.get("usage") if isinstance(obj, dict) else None
            if usage:
                model = self.server.last_model
                info = cost_from_usage(model, usage)
                self.server.on_turn(model, _normalize_usage(usage),
                                    info["cost"], info["tokens"]["total"])

    # ------------ routes ------------
    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            self._send_json(200, {"ok": True, "service": "whale-pet-proxy",
                                  "version": self.server.version})
            return
        if self.path.split("?")[0] == "/v1/models":
            r = self._forward(self.path)
            if not r.get("ok"):
                self._send_json(r.get("status", 502), {"error": "上游请求失败"})
                return
            self.send_response(r["status"])
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(r["body"])))
            self.end_headers()
            self.wfile.write(r["body"])
            return
        # 其他 GET 透明转发
        r = self._forward(self.path)
        self.send_response(r.get("status", 502))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = r.get("body", b"{}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.startswith("/v1/"):
            self._send_json(404, {"error": {"message": "not found", "type": "not_found"}})
            return
        body = self._read_body()
        # 解析请求体：记录 model（用于 pricing）并精确判断是否流式
        self.server.last_model = ""
        is_stream = False
        try:
            if body:
                req = json.loads(body.decode("utf-8", "replace"))
                self.server.last_model = str(req.get("model") or "")
                is_stream = bool(req.get("stream"))
        except ValueError:
            pass
        self.server._pending_stream = is_stream
        r = self._forward(self.path, body, "POST")
        if not r.get("ok"):
            self._send_json(r.get("status", 502),
                            {"error": {"message": "上游请求失败"}})
            return
        if is_stream:
            self._relay_stream(r["resp"])
        else:
            self._relay_json(r.get("status", 502), r.get("headers", {}), r.get("body", b"{}"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


class ProxyServer:
    """桌宠内置中转服务。在独立线程运行，进程退出时自动关闭。"""

    def __init__(self, api_key: str, api_base: str = "https://api.deepseek.com",
                 host: str = "127.0.0.1", port: int = 11434,
                 on_turn: Callable[[str, dict, float, int], None] | None = None,
                 timeout: float = 300.0):
        self.api_key = api_key
        self.api_base = api_base
        self.host = host
        self.port = port
        self.on_turn = on_turn
        self.timeout = timeout
        self.last_model = ""
        self.version = "0.3"            # 与发布标签保持一致（/healthz 返回）
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError:
            return False
        # 把代理配置挂到 httpd 上，供 handler 通过 self.server 访问
        self._httpd.api_key = self.api_key
        self._httpd.api_base = self.api_base
        self._httpd.timeout = self.timeout
        self._httpd.on_turn = self.on_turn
        self._httpd.last_model = ""
        self._httpd._pending_stream = False
        self._httpd.version = self.version
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="whale-pet-proxy", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """关闭服务（幂等、异常安全）。线程为 daemon，进程退出不依赖它。"""
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"
