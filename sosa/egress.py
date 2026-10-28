#!/usr/bin/env python3
"""egress.py: the host end of a box's way out. sosad starts one beside each box's cloud-hypervisor.

Listens on the box's `vsock_3128` unix socket, where cloud-hypervisor delivers the guest's vsock connections to
host port 3128 (the guest's fwd.py relays 127.0.0.1:3128 there). An HTTP proxy that allows:
  - CONNECT host:port, for hosts on the allow list (the stack's [egress] allow, plus the model path)
  - plain absolute-form requests (GET http://host/...) to allowed hosts, which apt and the like need: only with
    --allow-plain (a bake), since the proxy then sees the request
Anything else gets a 403 and a REFUSED line in the log, so you can see what else the box wanted. With --allow-all
(a bake, which is your own stack downloading its toolchain) everything goes.
"""
import argparse
import os
import socket
import sys
import threading
import time
import urllib.parse

MAX_HEAD = 64 << 10


class Log:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1) if path else None
        self.mu = threading.Lock()

    def __call__(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        with self.mu:
            print(line, flush=True)
            if self.f:
                self.f.write(line + "\n")


def pump(src, dst, counter, i):
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            counter[i] += len(b)
            dst.sendall(b)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class Egress:
    def __init__(self, allow, allow_all, allow_plain, log):
        self.allow = {a.lower() for a in allow}
        self.allow_all = allow_all
        self.allow_plain = allow_plain
        self.log = log

    def permitted(self, host, port):
        if self.allow_all:
            return True
        hp = f"{host}:{port}".lower()
        if hp in self.allow:
            return True
        # "*.example.com:443" allows any subdomain
        return any(a.startswith("*.") and hp.endswith(a[1:]) for a in self.allow)

    def refuse(self, c, first, why=""):
        self.log(f"REFUSED {first[:300]}{(' (' + why + ')') if why else ''}")
        msg = (b"sosa's egress allows only " + ", ".join(sorted(self.allow)).encode()
               + (b" (plain HTTP too)" if self.allow_plain else b" (CONNECT only)") + b"\n")
        try:
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: "
                      + str(len(msg)).encode() + b"\r\nConnection: close\r\n\r\n" + msg)
        except OSError:
            pass

    def handle(self, c):
        t0 = time.time()
        head = b""
        try:
            c.settimeout(30)
            while b"\r\n\r\n" not in head:
                b = c.recv(4096)
                if not b:
                    return
                head += b
                if len(head) > MAX_HEAD:
                    self.log("REFUSED oversized request head")
                    return
            head, _, rest = head.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            first = lines[0].decode("latin-1")
            parts = first.split()
            if len(parts) != 3:
                c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return self.log(f"REFUSED malformed {first[:200]!r}")
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                if not host or not port.isdigit() or not self.permitted(host, port):
                    return self.refuse(c, first)
                try:
                    u = socket.create_connection((host, int(port)), timeout=15)
                except OSError as e:
                    self.log(f"FAILED CONNECT {target}: {e}")
                    c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.bridge(c, u, rest, f"CONNECT {target}", t0)
            elif target.lower().startswith("http://"):
                if not self.allow_plain:
                    return self.refuse(c, first, "plain HTTP is off")
                url = urllib.parse.urlsplit(target)
                host, port = url.hostname, url.port or 80
                if not host or not self.permitted(host, port):
                    return self.refuse(c, first)
                try:
                    u = socket.create_connection((host, port), timeout=15)
                except OSError as e:
                    self.log(f"FAILED {method} {target}: {e}")
                    c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                # origin-form to the server, hop-by-hop proxy headers dropped, one request per connection
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                out = [f"{method} {path} {version}".encode()]
                for h in lines[1:]:
                    k = h.split(b":", 1)[0].strip().lower()
                    if k in (b"proxy-connection", b"proxy-authorization", b"connection"):
                        continue
                    out.append(h)
                out.append(b"Connection: close")
                u.sendall(b"\r\n".join(out) + b"\r\n\r\n")
                self.bridge(c, u, rest, f"{method} {target}", t0)
            else:
                return self.refuse(c, first)
        except OSError as e:
            self.log(f"error: {e}")
        finally:
            c.close()

    def bridge(self, c, u, rest, what, t0):
        u.settimeout(None)
        c.settimeout(None)
        counter = [0, 0]
        if rest:
            u.sendall(rest)
            counter[0] += len(rest)
        th = threading.Thread(target=pump, args=(u, c, counter, 1), daemon=True)
        th.start()
        pump(c, u, counter, 0)
        th.join()
        u.close()
        self.log(f"ALLOWED {what}: {counter[0]} B up, {counter[1]} B down, {time.time() - t0:.1f} s")


def serve(listen, allow, log_path=None, allow_all=False, allow_plain=False):
    log = Log(log_path)
    eg = Egress(allow, allow_all, allow_plain, log)
    try:
        os.unlink(listen)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        s.bind(listen)
    finally:
        os.umask(old)
    s.listen(64)
    log(f"egress listening on {listen}, allowing " + ("everything" if allow_all else ", ".join(sorted(eg.allow)))
        + (" (plain HTTP too)" if allow_plain else ""))
    while True:
        c, _ = s.accept()
        threading.Thread(target=eg.handle, args=(c,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--listen", required=True, help="the box's vsock_3128 socket path")
    ap.add_argument("--allow", action="append", default=[], help="host:port, or *.domain:port")
    ap.add_argument("--allow-all", action="store_true")
    ap.add_argument("--allow-plain", action="store_true", help="plain http:// requests too (a bake)")
    ap.add_argument("--log", help="also append to this file")
    a = ap.parse_args()
    try:
        serve(a.listen, a.allow or ["api.anthropic.com:443"], a.log, a.allow_all, a.allow_plain)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
