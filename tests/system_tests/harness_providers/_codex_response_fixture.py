# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loopback Responses fixture; drives the real CLI, never a remote model."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ResponsesFixture:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.items = []
        self.requests = []
        self.paths = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                owner.paths.append(self.path)
                if owner.status_code != 200:
                    data = json.dumps({"error": {
                        "message": "A0 fixture auth failure", "type": "authentication_error",
                    }}).encode()
                    self.send_response(owner.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                index = len(owner.requests)
                item = owner.items.pop(0) if owner.items else {
                    "type": "message", "role": "assistant", "id": f"msg_{index}", "status": "completed",
                    "content": [{"type": "output_text", "text": "A0-LOCAL-OK", "annotations": []}],
                }
                response = {"id": f"resp_{index}", "object": "response", "status": "in_progress", "output": []}
                events = [
                    ("response.created", {"response": response}),
                    ("response.output_item.added", {"output_index": 0, "item": item}),
                    ("response.output_item.done", {"output_index": 0, "item": item}),
                    ("response.completed", {"response": {
                        **response, "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }}),
                ]
                data = "".join(
                    f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n" for name, payload in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
