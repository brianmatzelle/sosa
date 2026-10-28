#!/usr/bin/env python3
"""sosa front: the front door. One address, a name per box, hosts that announce themselves, boxes that wake when
someone knocks.

    sosa front [--listen 127.0.0.1:8180] [--zone localhost] [--config ~/.sosa/front.json]

A request for http://NAME.ZONE/ goes to box NAME's first app port; http://NAME-PORT.ZONE/ to that port. If the
box is asleep (its idle timer stopped it) or down, the front door starts it, holds the connection until the mind
answers, then forwards: the browser sees a slow first page, not an error. Everything after the first line is a
byte pump, so WebSockets and streaming work, and the box's app sees the real Host and X-Forwarded-For/Host.

Hosts are daemons (sosad). They announce themselves: a host with <home>/join.json posts to /_sosa/register every
30 s and whenever a box changes, with its address, api token, boxes, stacks, images, running count and free
memory, and the front door keeps that in <home>/front-hosts.json (0600) so a restart routes at once. The
register secret is <home>/front-register-secret here (made on first run) and <home>/join-secret on each host
(`sosa join URL` asks for it). Hosts can also be fixed in front.json. The preview itself travels inside the
daemon's API (POST /v1/boxes/NAME/tunnel), so a host's preview ports never leave its loopback. Box names must be
unique across hosts; a second host announcing a name the first has is told so and that box is not routed.

On http://ZONE/ itself (or the front door by address, for the /_sosa/ paths), it is also the one address for a backend:
  GET  /_sosa/health                      the front door, its hosts
  GET  /_sosa/hosts                       every host: address, boxes, images, running, free memory, last seen
  GET  /_sosa/boxes                       every box with its host and URL
  POST /_sosa/register                    what hosts post (X-Sosa-Register: the secret)
  ANY  /_sosa/api/v1/boxes/NAME[/...]     the daemon's own route, on the host that has NAME (streams too)
  GET  /_sosa/api/v1/boxes                every host's boxes, merged (each with "host")
  POST /_sosa/api/v1/boxes {..,host?}     create on the named host, or on the host with the stack's image and
                                          the fewest boxes running (the answer carries "host")
  ANY  /_sosa/hosts/HOST/v1/...           any daemon route, on that host

Config, <home>/front.json (every key optional; flags override):
  {"listen": "127.0.0.1:8180", "zone": "localhost",
   "hosts": [{"name": "aws-1", "url": "http://10.0.0.5:8790", "token_file": "/home/me/.sosa/aws-1.token"}],
   "secret_file": "/home/me/.sosa/front-secret"}
With no hosts and none registered, the local daemon. With a secret_file, every request must carry
`X-Sosa-Front: <the secret>`: that's how you put your own login (SSO, a cookie, anything) in front of it: your
proxy checks the person, sets the header, and whatever it says in X-Sosa-User is passed on to the box's app;
without the secret those headers are stripped. The /_sosa/ routes take that secret or the register secret.
"""
import argparse
import http.client
import json
import os
import re
import secrets as secretsmod
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

HOME = os.path.abspath(os.path.expanduser(os.environ.get("SOSA_HOME") or "~/.sosa"))
MAX_HEAD = 64 << 10
MAX_BODY = 4 << 20
WAKE_WAIT = 300
STALE = 120               # seconds without a heartbeat before a host is shown as stale (it is still tried)
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
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

    def __init__(self, spec, log, registered=False):
        self.name = spec.get("name") or spec.get("url") or "local"
        self.url = spec.get("url") or f"unix://{HOME}/sosad.sock"
        self.token = spec.get("token")
        tf = spec.get("token_file")
        if tf and not self.token:
            self.token = open(os.path.expanduser(tf)).read().strip()
        self.log = log
        self.registered = registered
        self.info = {}             # what the host said about itself last
        self.seen = spec.get("seen") or 0
        self.boxes = {}            # name -> list entry
        self.fetched = 0
        self.mu = threading.Lock()

    def raw(self, timeout):
        """A connected socket to the daemon."""
        u = urllib.parse.urlsplit(self.url)
        if u.scheme == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(u.path)
            return s
        if u.scheme == "http":
            return socket.create_connection((u.hostname, u.port or 80), timeout)
        raise ValueError(f"host url must be unix:///path or http://host:port, not {self.url}")

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

    def announced(self, body):
        """A heartbeat: the host's own view of itself."""
        with self.mu:
            self.boxes = {b["name"]: b for b in body.get("boxes") or [] if isinstance(b, dict) and b.get("name") and not b.get("bake")}
            self.fetched = time.time()
            self.seen = time.time()
            self.info = {k: body.get(k) for k in ("stacks", "images", "running", "mem_free_mb", "cpus", "pid")}

    def stale(self):
        return self.registered and time.time() - self.seen > STALE

    def describe(self):
        return {"name": self.name, "url": self.url, "registered": self.registered, "stale": self.stale(),
                "seen": time.strftime("%FT%T", time.localtime(self.seen)) if self.seen else None,
                "boxes": sorted(self.boxes), **self.info}

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
    def __init__(self, cfg, home, log):
        self.cfg = cfg
        self.home = home
        self.log = log
        self.zone = (cfg.get("zone") or "localhost").lower().strip(".")
        self.fixed = [Host(h, log) for h in (cfg.get("hosts") or [])]
        self.dynamic = {}          # name -> Host, from /_sosa/register
        self.secret = None
        if cfg.get("secret_file"):
            self.secret = open(os.path.expanduser(cfg["secret_file"])).read().strip()
        rp = os.path.join(home, "front-register-secret")
        if not os.path.exists(rp):
            with open(os.open(rp, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                f.write(secretsmod.token_urlsafe(32) + "\n")
        self.register_secret = open(rp).read().strip()
        self.registry = os.path.join(home, "front-hosts.json")
        self.load_registry()
        self.implicit = not self.fixed and not self.dynamic
        if self.implicit:                      # nothing configured, nobody registered: the daemon on this machine
            self.fixed = [Host({"name": "local", "url": f"unix://{home}/sosad.sock"}, log)]
        self.wake_locks = {}
        self.mu = threading.Lock()

    # -- hosts
    def hosts(self):
        return self.fixed + list(self.dynamic.values())

    def host(self, name):
        return next((h for h in self.hosts() if h.name == name), None)

    def load_registry(self):
        try:
            with open(self.registry) as f:
                for spec in json.load(f).get("hosts") or []:
                    h = Host(spec, self.log, registered=True)
                    h.info = spec.get("info") or {}
                    h.boxes = {n: {"name": n} for n in spec.get("boxes") or []}
                    self.dynamic[h.name] = h
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.log(f"{self.registry}: {e}")

    def save_registry(self):
        rows = [{"name": h.name, "url": h.url, "token": h.token, "seen": h.seen, "info": h.info, "boxes": sorted(h.boxes)}
                for h in self.dynamic.values()]
        tmp = self.registry + ".new"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"hosts": rows}, f)
        os.replace(tmp, self.registry)

    def register(self, body):
        name = str(body.get("name") or "")
        url = str(body.get("url") or "")
        if not HOST_RE.match(name):
            raise ValueError("a host name is [a-z0-9-], up to 32")
        if not (url.startswith("http://") or url.startswith("unix://")):
            raise ValueError("url must be http://host:port or unix:///path")
        if url.startswith("http://") and not body.get("token"):
            raise ValueError("a TCP address needs the host's api token")
        fixed = next((h for h in self.fixed if h.name == name), None)
        if fixed:
            raise ValueError(f"{name} is a fixed host in front.json; it can't register")
        with self.mu:
            if self.implicit:                  # a real host: the implicit local one steps aside
                self.fixed, self.implicit = [], False
            h = self.dynamic.get(name)
            if h is None or h.url != url or h.token != body.get("token"):
                h = Host({"name": name, "url": url, "token": body.get("token")}, self.log, registered=True)
                self.dynamic[name] = h
                self.log(f"host {name} registered at {url}")
            h.announced(body)
            # a box name two hosts claim: the first keeps it
            dup = []
            for other in self.hosts():
                if other is h:
                    continue
                for n in list(h.boxes):
                    if n in other.boxes:
                        dup.append((n, other.name))
                        del h.boxes[n]
            self.save_registry()
        for n, o in dup:
            self.log(f"host {name}: box {n} is already on {o}; not routed")
        return {"ok": True, "name": name, "zone": self.zone, "hosts": len(self.hosts()),
                "duplicates": [f"{n} (on {o})" for n, o in dup]}

    # -- names
    def find(self, name):
        """(host, entry) for a box name, asking the hosts again if it isn't in the lists we have."""
        for fresh in (False, True):
            for h in self.hosts():
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
        for h in self.hosts():
            for n, b in sorted(h.refresh().items()):
                out.append({"name": n, "host": h.name, "url": f"http://{n}.{self.zone}/", "stack": b.get("stack"),
                            "running": b.get("running"), "asleep": b.get("asleep"), "idle": b.get("idle"),
                            "ports": b.get("ports") or []})
        return out

    def pick(self, stack, want=None):
        """The host to create a box on: the named one, else the one with the stack's image and the fewest running."""
        if want:
            h = self.host(want)
            if h is None:
                raise ValueError(f"no host {want} (GET /_sosa/hosts)")
            return h
        best = None
        for h in self.hosts():
            if h.stale():
                continue
            imgs = h.info.get("images")
            if imgs is not None and stack not in imgs:
                continue
            if imgs is None and stack not in (h.info.get("stacks") or []) and h.info:
                continue
            key = (h.info.get("running") if h.info.get("running") is not None else len([b for b in h.boxes.values() if b.get("running")]),
                   -(h.info.get("mem_free_mb") or 0))
            if best is None or key < best[0]:
                best = (key, h)
        if best is None:
            raise ValueError(f"no host has an image for stack {stack!r} (sosa image {stack} on one, or sosa stack add + image)")
        return best[1]

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
            path = req.split(" ")[1] if " " in req else "/"
            if name == "" or (name is None and path.startswith("/_sosa/")):
                return self.apex(c, peer, req, hdrs, rest, get)   # the zone itself, or the front door called by address
            if name is None:
                return self.reply(c, 404, f"no such address: {hostname or '(no Host header)'} is not under {self.zone}")
            if self.secret is not None and get("x-sosa-front") != self.secret:
                return self.reply(c, 403, "the front door is closed: this request didn't come through your login")
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

    # -- the apex: the front door's own routes, and the daemons' API behind one address
    def read_body(self, c, rest, get):
        n = int(get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("the body is too large")
        while len(rest) < n:
            chunk = c.recv(min(65536, n - len(rest)))
            if not chunk:
                break
            rest += chunk
        return rest

    def apex(self, c, peer, req, hdrs, rest, get):
        parts = req.split(" ")
        method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")
        url = urllib.parse.urlsplit(path)
        p = [x for x in url.path.split("/") if x]
        if p[:1] != ["_sosa"]:
            return self.reply(c, 200, f"sosa front door for *.{self.zone}: a box is http://NAME.{self.zone}/; GET /_sosa/boxes lists them")
        p = p[1:]
        if p == ["register"]:
            if method != "POST":
                return self.reply(c, 405, "POST")
            if get("x-sosa-register") != self.register_secret:
                return self.reply(c, 403, "the register secret is wrong (front-register-secret on the front door; join-secret on the host)")
            try:
                body = json.loads(self.read_body(c, rest, get) or b"{}")
                ans = self.register(body if isinstance(body, dict) else {})
            except ValueError as e:
                return self.reply(c, 400, json.dumps({"error": str(e)}), "application/json")
            return self.reply(c, 200, json.dumps(ans), "application/json")
        ok = get("x-sosa-register") == self.register_secret or self.secret is None or get("x-sosa-front") == self.secret
        if not ok:
            return self.reply(c, 403, "the front door is closed: this request didn't come through your login")
        if p == ["health"]:
            return self.reply(c, 200, json.dumps({"ok": True, "zone": self.zone, "hosts": [h.name for h in self.hosts()],
                                                  "stale": [h.name for h in self.hosts() if h.stale()]}), "application/json")
        if p == ["hosts"]:
            return self.reply(c, 200, json.dumps({"hosts": [h.describe() for h in self.hosts()]}), "application/json")
        if p == ["boxes"]:
            return self.reply(c, 200, json.dumps({"boxes": self.routes()}, ensure_ascii=False), "application/json")
        if len(p) >= 3 and p[0] == "hosts" and p[2] == "v1":
            h = self.host(p[1])
            if h is None:
                return self.reply(c, 404, json.dumps({"error": f"no host {p[1]}"}), "application/json")
            return self.proxy(c, peer, h, method, "/" + "/".join(p[2:]) + (("?" + url.query) if url.query else ""), hdrs, rest)
        if p[:3] == ["api", "v1", "boxes"]:
            if len(p) == 3 and method == "GET":
                rows = []
                for h in self.hosts():
                    rows += [{**b, "host": h.name} for b in h.refresh().values()]
                return self.reply(c, 200, json.dumps({"boxes": rows}, ensure_ascii=False), "application/json")
            if len(p) == 3 and method == "POST":
                try:
                    rest = self.read_body(c, rest, get)
                    body = json.loads(rest or b"{}")
                    body = body if isinstance(body, dict) else {}
                    h = self.pick(str(body.get("stack") or ""), body.get("host"))
                except ValueError as e:
                    return self.reply(c, 400, json.dumps({"error": str(e)}), "application/json")
                self.log(f"{peer}: create {body.get('name')} ({body.get('stack')}) on {h.name}")
                return self.proxy(c, peer, h, method, "/v1/boxes", hdrs, rest, tag=h.name)
            name = p[3]
            h, _ = self.find(name)
            if h is None:
                return self.reply(c, 404, json.dumps({"error": f"no box named {name} on any host"}), "application/json")
            return self.proxy(c, peer, h, method, "/" + "/".join(p[1:]) + (("?" + url.query) if url.query else ""), hdrs, rest)
        return self.reply(c, 404, json.dumps({"error": f"no route {method} {url.path} (front.py lists them)"}), "application/json")

    def proxy(self, c, peer, host, method, path, hdrs, rest, tag=None):
        """One daemon request, passed through byte for byte (so NDJSON streams), with the host's own token."""
        try:
            s = host.raw(WAKE_WAIT)
        except OSError as e:
            return self.reply(c, 502, json.dumps({"error": f"{host.name}: {e}"}), "application/json")
        drop = ("authorization", "x-sosa-front", "x-sosa-register", "host", "connection")
        keep = [(k, v) for k, v in hdrs if k.lower() not in drop]
        keep += [("Host", "sosad"), ("Connection", "close")]
        if host.token:
            keep.append(("Authorization", f"Bearer {host.token}"))
        if tag:
            keep.append(("X-Sosa-Host", tag))
        out = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in keep) + "\r\n"
        s.sendall(out.encode("iso-8859-1") + rest)
        self.log(f"{peer} api {method} {path} -> {host.name}")
        self.pump(c, s)

    def reply(self, c, code, text, ctype="text/plain; charset=utf-8", retry=None):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                  431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}.get(code, "Error")
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
    log = Logger(os.path.join(home, "front.log"))
    front = Front(cfg, home, log)
    listen = cfg.get("listen") or "127.0.0.1:8180"
    host, _, port = listen.rpartition(":")

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            front.serve(self.request, self.client_address[0])

    srv = Server((host or "127.0.0.1", int(port)), H)
    log(f"front door on {listen} for *.{front.zone}; hosts: {', '.join(h.name for h in front.hosts()) or 'none yet'}"
        + ("; secret header required" if front.secret is not None else "")
        + f"; hosts register with {os.path.join(home, 'front-register-secret')}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
