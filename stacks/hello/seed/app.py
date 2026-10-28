#!/usr/bin/env python3
"""The hello app: a web server from the standard library. Edit freely; `app load` swaps the new one in live."""
import http.server
import json
import os
import time

PORT = int(os.environ.get("PORT") or 8080)
STARTED = time.time()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, json.dumps({"ok": True, "uptime_s": round(time.time() - STARTED, 1), "port": PORT}),
                             "application/json")
        if self.path == "/":
            return self.send(200, f"hello from {os.environ.get('APP_NAME', 'the box')} on port {PORT}\n")
        self.send(404, "not found\n")


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
