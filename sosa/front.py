#!/usr/bin/env python3
"""sosa front: the front door. One address, a name per box, and boxes that wake when someone knocks.

    sosa front [--listen 127.0.0.1:8180] [--zone localhost] [--config ~/.sosa/front.json]

A request for http://NAME.ZONE/ goes to box NAME's first app port; http://NAME-PORT.ZONE/ to that port. If the
box is asleep (its idle timer stopped it) or down, the front door starts it, holds the connection until the mind
answers, then forwards: the browser sees a slow first page, not an error. Everything after the first line is a
byte pump, so WebSockets and streaming work, and the box's app sees the real Host and X-Forwarded-For/Host.

Boxes are found on hosts: daemons (sosad), reached by their unix socket or their TCP address and bearer token. The
preview itself travels inside the daemon's API (POST /v1/boxes/NAME/tunnel), so a host's preview ports never
leave its loopback and one front door can stand in front of many hosts. Names must be unique across hosts.

Config, <home>/front.json (every key optional; flags override):
  {"listen": "127.0.0.1:8180", "zone": "localhost",
   "hosts": [{"name": "local", "url": "unix:///home/me/.sosa/sosad.sock"},
             {"name": "aws-1", "url": "http://10.0.0.5:8790", "token_file": "/home/me/.sosa/aws-1.token"}],
   "secret_file": "/home/me/.sosa/front-secret"}
With no hosts, the local daemon. With a secret_file, every request must carry `X-Sosa-Front: <the secret>`:
that's how you put your own login (SSO, a cookie, anything) in front of it: your proxy checks the person, sets
the header, and whatever it says in X-Sosa-User is passed on to the box's app; without the secret those headers
are stripped. On http://ZONE/ itself: GET /_sosa/boxes (names, hosts, running or asleep) and GET /_sosa/health.
"""
import argparse
import http.client
import json
import os
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

HOME = os.path.abspath(os.path.expanduser(os.environ.get("SOSA_HOME") or "~/.sosa"))
MAX_HEAD = 64 << 10
WAKE_WAIT = 300
log_lock = threading.Lock()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Logger:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1)

    def __call__(self, msg):
        line = f"{now()} {msg}"
        with log_lock:
            print(line, file=sys.stderr, flush=True)
            self.f.write(line + "\n")


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("sosad", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Host:
    """One daemon."""

    def __init__(self, spec, log):
        self.name = spec.get("name") or spec.get("url") or "local"
        self.url = spec.get("url") or f"unix://{HOME}/sosad.sock"
        self.token = None
        tf = spec.get("token_file")
        if tf:
            self.token = open(os.path.expanduser(tf)).read().strip()
        self.log = log
        self.boxes = {}            # name -> list entry
        self.fetched = 0
        self.mu = threading.Lock()

    def conn(self, timeout):
        u = urllib.parse.urlsplit(self.url)
        if u.scheme == "unix":
            return UnixConn(u.path, timeout)
        if u.scheme == "http":
            return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        raise ValueError(f"host url must be unix:///path or http://host:port, not {self.url}")

    def headers(self, body=None):
        h = {"content-type": "application/json"} if body is not None else {}
        if self.token:
            h["authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method, path, body=None, timeout=60):
        c = self.conn(timeout)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=self.headers(body))
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            obj = json.loads(raw)
        except ValueError:
            raise OSError(f"{self.name}: {method} {path}: HTTP {r.status}: {raw[:200]!r}")
        if r.status != 200:
            raise OSError(obj.get("error") or f"{self.name}: HTTP {r.status}")
        return obj

    def refresh(self, max_age=5):
        with self.mu:
            if time.time() - self.fetched < max_age:
                return self.boxes
            try:
                self.boxes = {b["name"]: b for b in self.request("GET", "/v1/boxes", timeout=10)["boxes"] if not b.get("bake")}
                self.fetched = time.time()
            except (OSError, ValueError, KeyError) as e:
                self.log(f"{self.name}: box list: {e}")
            return self.boxes

    def tunnel(self, name, port, timeout=WAKE_WAIT):
        """A socket that is the box's port: the daemon's tunnel route after its 200 line."""
        c = self.conn(timeout)
        body = json.dumps({"port": port}).encode()
        c.request("POST", f"/v1/boxes/{name}/tunnel", body=body, headers=self.headers(body))
        r = c.getresponse()
        if r.status != 200:
            raw = r.read()
            c.close()
            try:
                raise OSError(json.loads(raw).get("error") or f"HTTP {r.status}")
            except ValueError:
                raise OSError(f"{self.name}: tunnel: HTTP {r.status}: {raw[:200]!r}")
        s = c.sock
        c.sock = None                   # ours now; http.client won't close it
        s.settimeout(None)
        return s


class Front:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.zone = (cfg.get("zone") or "localhost").lower().strip(".")
        self.hosts = [Host(h, log) for h in (cfg.get("hosts") or [{"name": "local"}])]
        self.secret = None
        if cfg.get("secret_file"):
            self.secret = open(os.path.expanduser(cfg["secret_file"])).read().strip()
        self.wake_locks = {}
        self.mu = threading.Lock()

    # -- names
    def find(self, name):
        """(host, entry) for a box name, asking the hosts again if it isn't in the lists we have."""
        for fresh in (False, True):
            for h in self.hosts:
                b = (h.refresh(0 if fresh else 5)).get(name)
                if b:
                    return h, b
        return None, None

    def resolve(self, hostname):
        """'NAME.zone' -> (NAME, None); 'NAME-PORT.zone' -> (NAME, PORT); the zone itself -> ('', None)."""
        hn = hostname.lower().split(":")[0].strip(".")
        if hn == self.zone:
            return "", None
        if not hn.endswith("." + self.zone):
            return None, None
        label = hn[: -len(self.zone) - 1]
        if "." in label:
            return None, None
        name, _, port = label.rpartition("-")
        if name and port.isdigit():
            return name, int(port)
        return label, None

    def routes(self):
        out = []
        for h in self.hosts:
            for n, b in sorted(h.refresh().items()):
                out.append({"name": n, "host": h.name, "url": f"http://{n}.{self.zone}/", "stack": b.get("stack"),
                            "running": b.get("running"), "asleep": b.get("asleep"), "idle": b.get("idle"),
                            "ports": b.get("ports") or []})
        return out

    # -- a connection
    def serve(self, c, peer):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = c.recv(4096)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEAD:
                    return self.reply(c, 431, "the request head is too large")
            top, rest = head.split(b"\r\n\r\n", 1)
            lines = top.decode("iso-8859-1").split("\r\n")
            req = lines[0]
            hdrs = []
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                hdrs.append((k.strip(), v.strip()))
            get = lambda k: next((v for kk, v in hdrs if kk.lower() == k), "")  # noqa: E731
            hostname = get("host")
            name, port = self.resolve(hostname)
            if name is None:
                return self.reply(c, 404, f"no such address: {hostname or '(no Host header)'} is not under {self.zone}")
            if self.secret is not None and get("x-sosa-front") != self.secret:
                return self.reply(c, 403, "the front door is closed: this request didn't come through your login")
            if name == "":
                return self.apex(c, req)
            host, entry = self.find(name)
            if host is None:
                return self.reply(c, 404, f"no box named {name} on any host")
            ports = entry.get("ports") or []
            if not ports:
                return self.reply(c, 404, f"{name} runs no app")
            port = port or ports[0]
            if port not in ports:
                return self.reply(c, 404, f"{name}'s ports are {ports}")
            try:
                up = self.wake(host, name, entry)
            except OSError as e:
                return self.reply(c, 503, f"{name} didn't come up: {e}", retry=30)
            try:
                v = host.tunnel(name, port)
            except OSError as e:
                return self.reply(c, 502, f"{name}: {e}", retry=10)
            # forward the head with the proxy headers set (ours, never the client's)
            keep = [(k, val) for k, val in hdrs if k.lower() not in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                                                                      "x-sosa-front") and (self.secret is not None or k.lower() != "x-sosa-user")]
            keep += [("X-Forwarded-For", peer), ("X-Forwarded-Host", hostname), ("X-Forwarded-Proto", "http")]
            out = req + "\r\n" + "".join(f"{k}: {val}\r\n" for k, val in keep) + "\r\n"
            v.sendall(out.encode("iso-8859-1") + rest)
            self.log(f"{peer} {hostname} {req.split(' ')[0]} {req.split(' ')[1] if ' ' in req else ''} -> {host.name}/{name}:{port}"
                     + (" (woken)" if up else ""))
            self.pump(c, v)
        except (OSError, ValueError, UnicodeError) as e:
            self.log(f"{peer}: {type(e).__name__}: {e}")
        finally:
            try:
                c.close()
            except OSError:
                pass

    def wake(self, host, name, entry):
        """Starts the box if it isn't running. True when this request woke it."""
        if entry.get("running"):
            return False
        with self.mu:
            lk = self.wake_locks.setdefault(name, threading.Lock())
        with lk:                               # the second knock waits for the first start, not a second one
            b = host.refresh(0).get(name) or {}
            if b.get("running"):
                return False
            self.log(f"{name}: waking on {host.name}" + (" (asleep since " + b["asleep"] + ")" if b.get("asleep") else ""))
            t = time.time()
            host.request("POST", f"/v1/boxes/{name}/start", {}, timeout=WAKE_WAIT)
            host.fetched = 0
            self.log(f"{name}: up in {time.time() - t:.0f}s")
            return True

    def pump(self, a, b):
        def one(src, dst):
            try:
                while True:
                    d = src.recv(65536)
                    if not d:
                        break
                    dst.sendall(d)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=one, args=(b, a), daemon=True)
        t.start()
        one(a, b)
        t.join()
        b.close()

    def apex(self, c, req):
        path = req.split(" ")[1] if " " in req else "/"
        if path.startswith("/_sosa/health"):
            return self.reply(c, 200, json.dumps({"ok": True, "zone": self.zone, "hosts": [h.name for h in self.hosts]}), "application/json")
        if path.startswith("/_sosa/boxes"):
            return self.reply(c, 200, json.dumps({"boxes": self.routes()}, ensure_ascii=False), "application/json")
        return self.reply(c, 200, f"sosa front door for *.{self.zone}: a box is http://NAME.{self.zone}/; GET /_sosa/boxes lists them")

    def reply(self, c, code, text, ctype="text/plain; charset=utf-8", retry=None):
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 431: "Request Header Fields Too Large",
                  502: "Bad Gateway", 503: "Service Unavailable"}.get(code, "Error")
        body = text.encode("utf-8") + (b"" if text.endswith("\n") else b"\n")
        h = f"HTTP/1.1 {code} {reason}\r\ncontent-type: {ctype}\r\ncontent-length: {len(body)}\r\nconnection: close\r\n"
        if retry:
            h += f"retry-after: {retry}\r\n"
        try:
            c.sendall(h.encode() + b"\r\n" + body)
        except OSError:
            pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=HOME)
    ap.add_argument("--config", help="front.json (default <home>/front.json)")
    ap.add_argument("--listen")
    ap.add_argument("--zone")
    a = ap.parse_args()
    home = os.path.abspath(os.path.expanduser(a.home))
    cfgp = a.config or os.path.join(home, "front.json")
    cfg = {}
    if os.path.exists(cfgp):
        with open(cfgp) as f:
            cfg = json.load(f)
    if a.listen:
        cfg["listen"] = a.listen
    if a.zone:
        cfg["zone"] = a.zone
    for h in cfg.get("hosts") or []:
        if h.get("url", "").startswith("unix://"):
            h["url"] = "unix://" + os.path.expanduser(h["url"][7:])
    if not cfg.get("hosts"):
        cfg["hosts"] = [{"name": "local", "url": f"unix://{home}/sosad.sock"}]
    log = Logger(os.path.join(home, "front.log"))
    front = Front(cfg, log)
    listen = cfg.get("listen") or "127.0.0.1:8180"
    host, _, port = listen.rpartition(":")

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            front.serve(self.request, self.client_address[0])

    srv = Server((host or "127.0.0.1", int(port)), H)
    log(f"front door on {listen} for *.{front.zone}; hosts: {', '.join(h.name for h in front.hosts)}"
        + ("; secret header required" if front.secret is not None else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
