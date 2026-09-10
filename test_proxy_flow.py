"""中转服务完整链路测试：假上游 -> ProxyServer -> 统计回调。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pet.proxy import ProxyServer


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n))
        if req.get("stream"):
            data = [
                {"id": "x", "choices": [{"delta": {"content": "hi"}}]},
                {"id": "x", "choices": [{"delta": {}}], "usage": {
                    "prompt_tokens": 1000, "completion_tokens": 500,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 100}}},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for d in data:
                self.wfile.write(("data: " + json.dumps(d) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500,
                          "prompt_tokens_details": {"cached_tokens": 0},
                          "completion_tokens_details": {"reasoning_tokens": 100}},
            }).encode())


def main():
    up = ThreadingHTTPServer(("127.0.0.1", 18080), FakeUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    calls = []
    proxy = ProxyServer(api_key="test-key", api_base="http://127.0.0.1:18080",
                        port=19000,
                        on_turn=lambda m, u, c, t: calls.append((m, u, c, t)))
    assert proxy.start(), "proxy start failed"
    time.sleep(0.3)

    with urllib.request.urlopen("http://127.0.0.1:19000/healthz") as r:
        print("healthz:", r.status, json.loads(r.read())["ok"])

    req = urllib.request.Request(
        "http://127.0.0.1:19000/v1/chat/completions",
        data=json.dumps({"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read())
        print("non-stream content:", out["choices"][0]["message"]["content"])

    req2 = urllib.request.Request(
        "http://127.0.0.1:19000/v1/chat/completions",
        data=json.dumps({"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": "hi"}],
                         "stream": True}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req2, timeout=10) as r:
        body = r.read().decode()
        print("stream has DONE:", "[DONE]" in body,
              "| has usage:", "usage" in body)

    time.sleep(0.5)
    print("turn callbacks:", len(calls))
    for m, u, c, t in calls:
        print("  model:", m, "| cost %.4f" % c, "| tokens:", t)
    proxy.stop()
    up.shutdown()
    print("ALL PROXY TESTS PASSED")


if __name__ == "__main__":
    main()
