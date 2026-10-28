#!/usr/bin/env python3
"""relay.py: Claude for the boxes, with the key none of them ever sees.

A box reaches it on vsock port 8097 (its 127.0.0.1:8097, guest fwd.py): cloud-hypervisor hands that connection
to <boxes>/<name>/vsock_8097, a socket this binds for every box it finds, so the socket a request comes in on
says which box is asking. Claude Code in the box runs with ANTHROPIC_BASE_URL pointed here and a placeholder key
(mind.py: "relay http://127.0.0.1:8097" where a token would be); the relay puts in the real key and forwards to
api.anthropic.com.

The box is the agent's, and its Bash reaches this port as well as Claude Code does, so what gets through is bounded:
  - POST /v1/messages and /v1/messages/count_tokens only, 32 MB a request, 4 at once per box
  - a model from PRICES (anything else becomes the default) and at most 32k tokens out; no fast mode, server-side
    tools (web search, code execution), MCP servers, containers or service tiers
  - what each answer cost, from its usage, in <home>/usage.jsonl; once a box has spent its day's budget, or all of
    them together have, it answers 429 until midnight

    relay.py serve --boxes ~/.sosa/boxes --key-file ~/.sosa/anthropic-key [--workspace-file F] [--box-day 20] [--day 100]
    relay.py spend [days] --usage ~/.sosa/usage.jsonl

The key may be an API key (sk-ant-api...) or a Claude Code OAuth token from `claude setup-token` (sk-ant-oat...),
sent the way Claude Code sends it; with a token the dollar figures are notional.
"""
import argparse
import collections
import http.client
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse

UPSTREAM = "api.anthropic.com"
SOCKET = "vsock_8097"
# $ per million tokens: input, output, cache read, 5-minute cache write (1-hour writes are twice input).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (4.0, 20.0, 0.20, 5.0),
}
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_OUT = 32000
MAX_BODY = 32 << 20
MAX_OPEN = 4
PATH_RE = re.compile(r"^/v1/messages(/count_tokens)?$")
DROP_FIELDS = ("mcp_servers", "container", "service_tier", "speed", "inference_geo")
SERVER_TOOL = re.compile(r"^(web_search|web_fetch|code_execution|advisor|mcp_toolset)")
NAME = re.compile(r"^[a-z0-9-]{1,32}$")
KEEP_HEADERS = ("content-type", "accept", "anthropic-version")

lock = threading.Lock()
spent = collections.defaultdict(float)  # (day, box) -> $, and (day, "") -> everyone's
opened = collections.Counter()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


class Config:
    key = ""
    workspace = ""
    boxes = ""
    usage = ""
    box_day = 20.0
    day = 100.0
    default_model = DEFAULT_MODEL
    pin_model = ""        # every request becomes this model, if set


CFG = Config()


def load_spent():
    day = time.strftime("%F")
    try:
        with open(CFG.usage) as f:
            for line in f:
                if line.startswith('{"day": "' + day):
                    u = json.loads(line)
                    spent[(day, u["box"])] += u.get("cost_usd") or 0
                    spent[(day, "")] += u.get("cost_usd") or 0
    except OSError:
        pass


class Usage:
    def __init__(self, model):
        self.price = PRICES.get(model) or PRICES[DEFAULT_MODEL]
        self.n = [0, 0, 0, 0, 0]   # in, out, cache read, 5-minute write, 1-hour write
        self.buf = b""

    def add(self, u):
        self.n[0] += int(u.get("input_tokens") or 0)
        self.n[1] += int(u.get("output_tokens") or 0)
        self.n[2] += int(u.get("cache_read_input_tokens") or 0)
        split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
        if split:
            self.n[3] += int(split.get("ephemeral_5m_input_tokens") or 0)
            self.n[4] += int(split.get("ephemeral_1h_input_tokens") or 0)
        else:
            self.n[3] += int(u.get("cache_creation_input_tokens") or 0)

    def event(self, ev):
        if ev.get("type") == "message_start":
            self.add((ev.get("message") or {}).get("usage") or {})
        elif ev.get("type") == "message_delta":
            self.n[1] += int((ev.get("usage") or {}).get("output_tokens") or 0)

    def sse(self, data):
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                try:
                    self.event(json.loads(line[5:]))
                except ValueError:
                    pass

    def cost(self):
        i, o, r, w5 = self.price
        return (self.n[0] * i + self.n[1] * o + self.n[2] * r + self.n[3] * w5 + self.n[4] * 2 * i) / 1e6


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def address_string(self):
        return self.server.box

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, message):
        kind = {400: "invalid_request_error", 404: "not_found_error", 413: "request_too_large",
                429: "rate_limit_error"}.get(code, "api_error")
        self.reply(code, {"type": "error", "error": {"type": kind, "message": message}})

    def do_GET(self):
        if self.path == "/health":
            day = time.strftime("%F")
            with lock:
                return self.reply(200, {"ok": True, "box": self.server.box, "spent_today": round(spent[(day, self.server.box)], 4),
                                        "box_day_budget": CFG.box_day, "all_today": round(spent[(day, "")], 4),
                                        "day_budget": CFG.day})
        self.error(404, "not found")

    def do_POST(self):
        box = self.server.box
        size = int(self.headers.get("content-length") or 0)
        if size > MAX_BODY:
            self.close_connection = True
            return self.error(413, f"a request may be {MAX_BODY >> 20} MB at most")
        with lock:
            busy = opened[box] >= MAX_OPEN
            if not busy:
                opened[box] += 1
        if busy:
            self.close_connection = True
            return self.error(429, f"at most {MAX_OPEN} requests at once")
        try:
            self.forward(box, self.rfile.read(size))
        finally:
            with lock:
                opened[box] -= 1

    def forward(self, box, body):
        url = urllib.parse.urlsplit(self.path)
        m = PATH_RE.match(url.path)
        if not m:
            return self.error(404, "not a Claude messages call")
        counting = bool(m.group(1))
        try:
            req = json.loads(body)
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            return self.error(400, "the body is not a JSON object")
        asked = str(req.get("model"))
        model = CFG.pin_model or (asked if asked in PRICES else CFG.default_model)
        req["model"] = model
        for k in DROP_FIELDS:
            req.pop(k, None)
        if isinstance(req.get("tools"), list):
            req["tools"] = [t for t in req["tools"]
                            if not (isinstance(t, dict) and SERVER_TOOL.match(str(t.get("type") or "")))]
            if not req["tools"]:
                req.pop("tools")
                req.pop("tool_choice", None)
        if not counting:
            try:
                req["max_tokens"] = max(1, min(int(req.get("max_tokens") or MAX_OUT), MAX_OUT))
            except (TypeError, ValueError):
                req["max_tokens"] = MAX_OUT
        streaming = bool(req.get("stream"))
        day = time.strftime("%F")
        if not counting:
            with lock:
                mine, all_ = spent[(day, box)], spent[(day, "")]
            if mine >= CFG.box_day or all_ >= CFG.day:
                return self.error(429, f"this box has spent its budget for today (${mine:.2f} of ${CFG.box_day:g}); "
                                       "more after midnight")
        fwd = {k: v for k, v in self.headers.items() if k.lower() in KEEP_HEADERS}
        betas = [b.strip() for b in (self.headers.get("anthropic-beta") or "").split(",")
                 if b.strip() and not b.strip().startswith("fast-mode")]
        if CFG.key.startswith("sk-ant-oat"):
            fwd["authorization"] = "Bearer " + CFG.key
            if "oauth-2025-04-20" not in betas:
                betas.append("oauth-2025-04-20")
        else:
            fwd["x-api-key"] = CFG.key
        if betas:
            fwd["anthropic-beta"] = ",".join(betas)
        fwd["host"] = UPSTREAM
        if CFG.workspace:
            fwd["anthropic-workspace-id"] = CFG.workspace
        out = json.dumps(req).encode()
        path = url.path + (f"?{url.query}" if url.query else "")
        t0 = time.time()
        try:
            up = http.client.HTTPSConnection(UPSTREAM, timeout=600)
            up.request("POST", path, body=out, headers=fwd)
            r = up.getresponse()
        except Exception as e:  # noqa: BLE001
            log(f"{box} {path}: {e!r}")
            return self.error(502, f"Claude unreachable: {e}")
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        u, whole = Usage(model), b""
        try:
            while chunk := r.read1(65536):
                if streaming:
                    u.sse(chunk)
                else:
                    whole += chunk
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            up.close()
        if counting:
            return
        if not streaming and r.status == 200:
            try:
                u.add(json.loads(whole).get("usage") or {})
            except ValueError:
                pass
        cost = u.cost()
        with lock:
            spent[(day, box)] += cost
            spent[(day, "")] += cost
            with open(CFG.usage, "a") as f:
                f.write(json.dumps({"day": day, "at": time.strftime("%FT%T"), "box": box, "model": model,
                                    "asked": asked, "status": r.status, "in": u.n[0], "out": u.n[1],
                                    "cache_read": u.n[2], "cache_write": u.n[3] + u.n[4],
                                    "cost_usd": round(cost, 6), "seconds": round(time.time() - t0, 1)}) + "\n")
        if r.status != 200:
            log(f"{box} {path} {model}: HTTP {r.status}")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, box):
        self.box = box
        super().__init__(path, Handler, bind_and_activate=False)

    def server_bind(self):
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        old = os.umask(0o077)
        try:
            self.socket.bind(self.server_address)
        finally:
            os.umask(old)
        self.inode = os.stat(self.server_address).st_ino


def watch():
    """A socket for every box there is, kept: a box made later gets one within a couple of seconds, and one
    whose socket went (its dir made again) gets it back."""
    servers = {}
    while True:
        try:
            names = [n for n in os.listdir(CFG.boxes) if NAME.match(n) and os.path.isdir(os.path.join(CFG.boxes, n))]
        except FileNotFoundError:
            names = []
        for n in names:
            path = os.path.join(CFG.boxes, n, SOCKET)
            s = servers.get(n)
            try:
                ok = s is not None and os.stat(path).st_ino == s.inode
            except FileNotFoundError:
                ok = False
            if ok:
                continue
            if s is not None:
                s.shutdown()
                s.server_close()
            try:
                s = Server(path, n)
                s.server_bind()
                s.server_activate()
            except OSError as e:
                log(f"{n}: can't listen on {path} ({e})")
                servers.pop(n, None)
                continue
            servers[n] = s
            threading.Thread(target=s.serve_forever, daemon=True).start()
            log(f"{n}: listening")
        for n in [n for n in servers if n not in names]:
            servers.pop(n).server_close()
            log(f"{n}: gone")
        time.sleep(2)


def spend(days, usage):
    by = collections.defaultdict(lambda: collections.defaultdict(float))
    since = time.strftime("%F", time.localtime(time.time() - 86400 * (days - 1)))
    try:
        with open(usage) as f:
            for line in f:
                u = json.loads(line)
                if u["day"] >= since:
                    by[u["day"]][u["box"]] += u.get("cost_usd") or 0
    except FileNotFoundError:
        pass
    for day in sorted(by):
        print(day, f"${sum(by[day].values()):.2f}", "  ".join(f"{k} ${v:.2f}" for k, v in sorted(by[day].items())))
    if not by:
        print("nothing spent")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--boxes", required=True)
    s.add_argument("--key-file", required=True)
    s.add_argument("--workspace-file")
    s.add_argument("--usage", help="usage.jsonl (default: next to the boxes dir)")
    s.add_argument("--box-day", type=float, default=20, help="$ a box a day")
    s.add_argument("--day", type=float, default=100, help="$ for every box together a day")
    s.add_argument("--default-model", default=DEFAULT_MODEL)
    s.add_argument("--pin-model", default="", help="every request becomes this model")
    p = sub.add_parser("spend")
    p.add_argument("days", nargs="?", type=int, default=7)
    p.add_argument("--usage", required=True)
    a = ap.parse_args()
    if a.cmd == "spend":
        return spend(a.days, a.usage)
    CFG.boxes = os.path.expanduser(a.boxes)
    CFG.usage = os.path.expanduser(a.usage or os.path.join(os.path.dirname(CFG.boxes.rstrip("/")), "usage.jsonl"))
    CFG.key = open(os.path.expanduser(a.key_file)).read().strip()
    if a.workspace_file and os.path.exists(os.path.expanduser(a.workspace_file)):
        w = open(os.path.expanduser(a.workspace_file)).read().strip()
        CFG.workspace = w.partition("=")[2] or w
    CFG.box_day, CFG.day, CFG.default_model, CFG.pin_model = a.box_day, a.day, a.default_model, a.pin_model
    if not CFG.key.startswith("sk-ant-"):
        raise SystemExit(f"relay: {a.key_file} doesn't look like a Claude API key or setup-token token")
    os.makedirs(CFG.boxes, exist_ok=True)
    load_spent()
    log(f"relay: ${CFG.box_day:g} a box a day, ${CFG.day:g} for everyone; {CFG.boxes}/*/{SOCKET}; "
        f"{'token' if CFG.key.startswith('sk-ant-oat') else 'api key'}")
    watch()


if __name__ == "__main__":
    main()
