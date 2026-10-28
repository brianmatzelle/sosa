#!/usr/bin/env python3
"""sosad: the daemon that owns the boxes. Everything else (the sosa CLI, your own backend) talks to it.

    sosad [--home ~/.sosa] [--tcp 127.0.0.1:8790]

It listens on <home>/sosad.sock (0600; the CLI's path) and, with --tcp, on that address too, where every request
must carry `Authorization: Bearer <home>/api-token`. It runs the boxes' cloud-hypervisors and egress proxies as
children and restarts one that dies; the Claude relay (relay.py), when <home>/anthropic-key exists; and the
preview listeners: 127.0.0.1:<box's preview port + i> -> the box's i-th app port, over vsock. Boxes with an idle
timer are stopped (and marked asleep) when nothing has touched them for that long; front.py wakes them on a request.

The API (JSON in, JSON out; the streaming calls answer NDJSON, one event a line):
  GET  /v1/health                        the daemon, how many boxes are up, the relay
  GET  /v1/stacks                        stacks sosa knows (<home>/stacks/*)
  POST /v1/stacks        {path}          copy a stack dir in (replacing one of the same name) and check it
  GET  /v1/stacks/{s}                    what's in it, and its problems
  GET  /v1/images                        baked images
  POST /v1/images        {stack}         bake the stack's image: streams log lines, ends with {"done":...}
  POST /v1/check         {stack,quick}   the contract test: streams, ends with {"done":true|false, "problems":[]}
  GET  /v1/boxes                         every box, running or not
  POST /v1/boxes         {name,stack,mem?,cpus?,disk?,auth?,owner?,idle?,start?}   make one (auth: relay | token;
                                         idle: minutes without a turn, exec or preview connection before it is
                                         stopped and marked asleep; 0 = never. A start wakes it; so does the front door)
  GET  /v1/boxes/{n}                     status: running, mind health, app, egress counts, disk
  DELETE /v1/boxes/{n}                   stop and delete it (snapshots too; the token is kept)
  POST /v1/boxes/{n}/start | stop        start waits for the mind; stop is the power button
  POST /v1/boxes/{n}/idle  {minutes}     change the idle timer
  POST /v1/boxes/{n}/tunnel {port,wake?} the connection becomes a raw bridge to that app port after a bare
                                         `HTTP/1.1 200` line: what front.py uses to reach a preview on any host.
                                         wake (default true): a box that is asleep is started first
  POST /v1/boxes/{n}/turn  {text,who?,role?,channel?,model?,effort?,max_turns?,max_budget_usd?}
         -> {"type":"facts",..} {"type":"delta","text"} {"type":"tool",..} {"type":"done",..} | {"type":"error",..}
  POST /v1/boxes/{n}/cancel | new        kill the turn under way | forget the session
  POST /v1/boxes/{n}/exec  {argv,user?,cwd?,timeout?,stdin?}   -> {rc,stdout,stderr}   (root in the box)
  POST /v1/boxes/{n}/push  {stack?:bool, claude?:bool}         ship the guest programs (and the stack) in
  POST /v1/boxes/{n}/token {token}       a `claude setup-token` token, for a box whose auth is token
  POST /v1/boxes/{n}/snapshot {label?} | GET /v1/boxes/{n}/snapshots | POST /v1/boxes/{n}/rollback {snapshot}
  GET/PUT /v1/boxes/{n}/prompt           the owner's prompt, sent with every turn (prompt.md on the host)
  GET  /v1/boxes/{n}/facts               what facts.sh prints now
  GET  /v1/boxes/{n}/transcript?n=20     the last turns
  GET  /v1/boxes/{n}/serial?n=50         the serial console's tail
  GET  /v1/usage?days=7                  what the relay counted, by day and box
"""
import argparse
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from sosa import relay as relaymod  # noqa: E402
from sosa import stack as stackmod  # noqa: E402
from sosa.box import Box, BoxError, Home, NAME_RE, PREVIEW_PORT  # noqa: E402
from sosa.box import mindpy  # noqa: E402

PREVIEW_BASE = 18000
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


class Running:
    def __init__(self, ch, egress):
        self.ch, self.egress = ch, egress
        self.since = time.time()
        self.stopping = False
        self.restarts = []
        self.previews = []            # listening sockets
        self.done = threading.Event() # set once the supervisor has cleaned up after the VM exited
        self.last_active = time.time()  # a turn, an exec from the API, a preview or tunnel connection


class Daemon:
    def __init__(self, home):
        self.home = home
        self.home.ensure()
        self.log = Logger(os.path.join(home.path, "sosad.log"))
        self.running = {}             # name -> Running
        self.locks = {}               # name -> Lock (lifecycle ops)
        self.mu = threading.Lock()
        self.relay_on = False
        self.harness = open(os.path.join(HERE, "harness.md")).read()
        threading.Thread(target=self.janitor, daemon=True).start()

    def touch(self, name):
        r = self.running.get(name)
        if r:
            r.last_active = time.time()

    def janitor(self):
        """Stops boxes whose idle timer has run out (and marks them asleep, so a daemon restart leaves them down)."""
        while True:
            time.sleep(30)
            for name, r in list(self.running.items()):
                try:
                    box = self.box(name)
                    idle = int(box.conf.get("idle") or 0)
                    if idle <= 0 or r.stopping or time.time() - r.last_active < idle * 60:
                        continue
                    h = box.health(3)
                    if h and (h.get("busy") or h.get("queued")):
                        r.last_active = time.time()
                        continue
                    self.log(f"{name}: idle for {idle} min; stopping (asleep)")
                    box.save_conf({**box.conf, "up": False, "asleep": time.strftime("%FT%T")})
                    self.stop(name, wanted_down=False)
                except (BoxError, OSError, ConnectionError, ValueError) as e:
                    self.log(f"{name}: idle check: {e}")

    def lock(self, name):
        with self.mu:
            return self.locks.setdefault(name, threading.Lock())

    # -- stacks
    def stack_for(self, box):
        c = box.conf
        p = os.path.join(self.home.stacks, c["stack"])
        if not os.path.exists(os.path.join(p, "stack.toml")):
            p = c.get("stack_path") or p
        return stackmod.load(p)

    def stacks(self):
        out = []
        for n in sorted(os.listdir(self.home.stacks)):
            try:
                s = stackmod.load(os.path.join(self.home.stacks, n))
                out.append({"name": s.name, "description": s.description, "path": s.path, "has_app": s.has_app,
                            "image": bool(self.home.stack_image(s.name)), "problems": s.problems()})
            except stackmod.StackError as e:
                out.append({"name": n, "problems": [str(e)]})
        return out

    def add_stack(self, path):
        s = stackmod.load(path)
        probs = s.problems()
        if probs:
            raise BoxError("the stack has problems: " + "; ".join(probs))
        dst = os.path.join(self.home.stacks, s.name)
        if os.path.abspath(path) != dst:
            tmp = dst + ".new"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(s.path, tmp, symlinks=False, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            shutil.rmtree(dst, ignore_errors=True)
            os.replace(tmp, dst)
        return stackmod.load(dst).describe()

    def images(self):
        out = []
        for n in sorted(os.listdir(self.home.images)):
            if n.endswith(".raw") and not n.startswith("noble-"):
                p = os.path.join(self.home.images, n)
                m = {}
                try:
                    m = json.load(open(p[:-4] + ".json"))
                except (OSError, ValueError):
                    pass
                out.append({"stack": n[:-4], "bytes": os.stat(p).st_blocks * 512, **m})
        return out

    # -- boxes
    def box(self, name):
        if not NAME_RE.match(name or ""):
            raise BoxError(f"not a box name: {name!r}")
        return Box(self.home, name)

    def free_preview_port(self):
        used = set()
        for n in self.home.names():
            try:
                c = Box(self.home, n).conf
                if c.get("preview_port"):
                    used.update(range(c["preview_port"], c["preview_port"] + max(1, len(c.get("ports") or []))))
            except BoxError:
                pass
        p = PREVIEW_BASE
        while p in used:
            p += 8
        return p

    def create(self, req):
        name = req.get("name") or ""
        box = self.box(name)
        with self.lock(name):
            if box.exists():
                raise BoxError(f"box {name} exists")
            sname = req.get("stack") or ""
            sp = os.path.join(self.home.stacks, sname)
            if not os.path.exists(os.path.join(sp, "stack.toml")):
                raise BoxError(f"no stack {sname!r} (sosa stack add <dir>; sosa stack list)")
            stack = stackmod.load(sp)
            probs = stack.problems()
            if probs:
                raise BoxError("the stack has problems: " + "; ".join(probs))
            auth = req.get("auth") or ("relay" if os.path.exists(os.path.join(self.home.path, "anthropic-key")) else "token")
            if auth not in ("relay", "token"):
                raise BoxError("auth is relay or token")
            cid = 3 + len(self.home.names())
            conf = box.create(stack, image=req.get("image"), mem=req.get("mem"), cpus=req.get("cpus"), disk=req.get("disk"),
                              auth=auth, owner=req.get("owner") or "", cid=cid,
                              preview_port=self.free_preview_port() if stack.has_app else None)
            idle = int(req.get("idle") or 0)
            if idle:
                box.save_conf({**box.conf, "idle": idle})
            self.log(f"{name}: created from {os.path.basename(conf['image'])} ({stack.name}), {conf['mem']}, "
                     f"{conf['cpus']} vcpu, {conf['disk']}, auth {auth}")
        if req.get("start"):
            self.start(name)
        return self.status(name)

    def start(self, name, wait=240):
        box = self.box(name)
        stack = self.stack_for(box)
        with self.lock(name):
            if name in self.running and box.running():
                return self.status(name)
            old = self.running.pop(name, None)
            if old is not None:
                old.stopping = True
                self.close_previews(old)
            c = box.conf
            allow = stack.egress_allow()
            ch, eg = box.launch(allow, allow_all=bool(c.get("bake")), allow_plain=bool(c.get("bake")))
            r = Running(ch, eg)
            self.running[name] = r
            threading.Thread(target=self.supervise, args=(name, r), daemon=True).start()
            self.log(f"{name}: starting (pid {ch.pid})")
            try:
                h = box.wait_mind(wait)
            except BoxError:
                if ch.poll() is not None:
                    err = open(os.path.join(box.dir, "ch.err"), "rb").read()[-800:].decode("utf-8", "replace")
                    raise BoxError(f"{name}: cloud-hypervisor exited {ch.returncode}: {err.strip()}")
                raise
            self.ensure_auth(box, h)
            self.open_previews(box, r)
            h = box.health(3) or h
            if not c.get("up") or c.get("asleep"):
                box.save_conf({k: v for k, v in {**box.conf, "up": True}.items() if k != "asleep"})
            self.log(f"{name}: up (claude {h.get('claude')}, auth {h.get('auth')})")
        return self.status(name)

    def start_wanted(self):
        """Boxes that were up when the daemon last ran come back (a reboot, a daemon restart)."""
        for n in self.home.names():
            try:
                box = Box(self.home, n)
                if box.conf.get("up") and not box.conf.get("bake"):
                    self.log(f"{n}: was up; starting")
                    self.start(n)
            except BoxError as e:
                self.log(f"{n}: not started: {e}")

    def ensure_auth(self, box, health):
        c = box.conf
        if c.get("bake"):
            return
        if c.get("auth") == "relay":
            if health.get("auth") != "relay":
                if not self.relay_on:
                    self.log(f"{box.name}: wants the relay, but there is no {self.home.path}/anthropic-key")
                box.set_token("relay http://127.0.0.1:8097")
        else:
            tok = None
            for p in (box.token_file, os.path.join(self.home.path, "token")):
                if os.path.exists(p):
                    tok = open(p).read().strip()
                    break
            if tok and health.get("auth") != "token":
                box.set_token(tok)
            elif not tok and not health.get("token"):
                self.log(f"{box.name}: no token yet (sosa token {box.name}, or {self.home.path}/token)")

    def supervise(self, name, r):
        rc = r.ch.wait()
        box = self.box(name)
        self.close_previews(r)
        try:
            r.egress.terminate()
        except OSError:
            pass
        if self.running.get(name) is r:
            box.cleanup_sockets()
            del self.running[name]
        r.done.set()
        if r.stopping:
            return
        self.log(f"{name}: cloud-hypervisor exited {rc} unexpectedly")
        r.restarts = [t for t in r.restarts if time.time() - t < 600]
        if len(r.restarts) >= 3:
            self.log(f"{name}: three exits in ten minutes; leaving it down")
            return
        time.sleep(3)
        try:
            self.start(name)
            self.running[name].restarts = r.restarts + [time.time()]
        except BoxError as e:
            self.log(f"{name}: restart failed: {e}")

    def stop(self, name, timeout=120, wanted_down=True):
        box = self.box(name)
        lk = self.lock(name)
        got = lk.acquire(timeout=30)      # a start still waiting for the mind holds it; the button is safe anyway
        try:
            r = self.running.get(name)
            if r:
                r.stopping = True
            if wanted_down and box.conf.get("up"):
                box.save_conf({**box.conf, "up": False})
            how = box.stop(timeout)
            if r:
                r.done.wait(15)       # the supervisor's cleanup (its preview sockets) before anything binds again
            self.log(f"{name}: {how}")
            return {"name": name, "result": how}
        finally:
            if got:
                lk.release()

    def destroy(self, name):
        box = self.box(name)
        with self.lock(name):
            r = self.running.get(name)
            if r:
                r.stopping = True
            box.destroy()
            if r:
                r.done.wait(15)
            self.log(f"{name}: destroyed")
        return {"name": name, "destroyed": True}

    def status(self, name):
        box = self.box(name)
        c = box.conf
        r = self.running.get(name)
        st = {"name": name, "stack": c["stack"], "running": box.running(), "mem": c["mem"], "cpus": c["cpus"],
              "disk": c["disk"], "auth": c["auth"], "owner": c.get("owner"), "created": c["created"],
              "disk_usage": box.disk_usage(), "snapshots": len(box.list_snapshots()), "egress": box.egress_counts(),
              "preview": None, "ports": c.get("ports") or [], "idle": int(c.get("idle") or 0), "asleep": c.get("asleep")}
        if r and not r.stopping:
            st["idle_for"] = int(time.time() - r.last_active)
        if r:
            st["since"] = time.strftime("%FT%T", time.localtime(r.since))
        if st["running"]:
            h = box.health(3)
            st["mind"] = h
            if c.get("ports") and c.get("preview_port"):
                st["preview"] = {str(p): f"http://127.0.0.1:{c['preview_port'] + i}" for i, p in enumerate(c["ports"])}
            if c.get("ports") and h:
                try:
                    ev = box.exec(["/opt/sosa/bin/sosa-app", "status", "--json"], 20)
                    st["app"] = json.loads(ev["stdout"]) if ev.get("rc") == 0 and ev.get("stdout", "").startswith("{") else {"error": (ev.get("stderr") or ev.get("stdout") or "")[-300:]}
                except (OSError, ConnectionError, ValueError) as e:
                    st["app"] = {"error": str(e)}
        return st

    def list_boxes(self):
        out = []
        for n in self.home.names():
            try:
                c = Box(self.home, n).conf
                out.append({"name": n, "stack": c["stack"], "running": n in self.running and Box(self.home, n).running(),
                            "auth": c["auth"], "owner": c.get("owner"), "created": c["created"], "bake": bool(c.get("bake")),
                            "ports": c.get("ports") or [], "idle": int(c.get("idle") or 0), "asleep": c.get("asleep")})
            except BoxError:
                pass
        return out

    # -- previews
    def open_previews(self, box, r):
        c = box.conf
        if not c.get("ports") or not c.get("preview_port"):
            return
        for i, gport in enumerate(c["ports"]):
            hport = c["preview_port"] + i
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", hport))
            except OSError as e:
                self.log(f"{box.name}: preview port {hport}: {e}")
                continue
            s.listen(64)
            r.previews.append(s)
            threading.Thread(target=self.preview_accept, args=(box, s, gport), daemon=True).start()
            self.log(f"{box.name}: preview 127.0.0.1:{hport} -> box port {gport}")

    def close_previews(self, r):
        for s in r.previews:
            # shutdown first: close() alone leaves the accept() thread blocked in the kernel and the port held
            for f in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
                try:
                    f()
                except OSError:
                    pass
        r.previews = []

    def preview_accept(self, box, s, gport):
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            self.touch(box.name)
            threading.Thread(target=self.preview_bridge, args=(box, c, gport), daemon=True).start()

    def preview_bridge(self, box, c, gport):
        try:
            v = mindpy.connect(f"hvsock:{box.vsock}:{PREVIEW_PORT}", 3600)
            v.sendall(f"PORT {gport}\n".encode())
            line = b""
            while not line.endswith(b"\n") and len(line) < 200:
                ch = v.recv(1)
                if not ch:
                    break
                line += ch
            if not line.startswith(b"OK"):
                msg = line.decode("utf-8", "replace").strip() or "the box didn't answer"
                c.sendall(f"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n{msg}\n".encode())
                return c.close()
        except (OSError, ConnectionError) as e:
            try:
                c.sendall(f"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nbox unreachable: {e}\n".encode())
            except OSError:
                pass
            return c.close()

        def pump(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except OSError:
                pass
            finally:
                try:
                    b.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=pump, args=(v, c), daemon=True)
        t.start()
        pump(c, v)
        t.join()
        c.close()
        v.close()

    # -- turns
    def facts(self, box, stack):
        if not stack.file("facts.sh"):
            return ""
        a = box.conf.get("agent") or stack.agent
        try:
            ev = box.exec(["bash", "/opt/sosa/stack/facts.sh"], 20, user=a["user"], cwd=a["cwd"])
            return (ev.get("stdout") or "").strip()[:4000]
        except (OSError, ConnectionError, ValueError) as e:
            return f"(facts.sh failed: {e})"

    def system_prompt(self, box, stack):
        a = box.conf.get("agent") or stack.agent
        s = self.harness.replace("{agent_name}", a["user"]).replace("{box}", box.name).replace("{stack}", stack.name)
        brief = stack.brief().strip()
        if brief:
            s += "\n\n" + brief
        if os.path.exists(box.prompt_file):
            owner = open(box.prompt_file).read().strip()
            if owner:
                s += "\n\n# From the owner\n\n" + owner
        return s

    def turn(self, name, req, send):
        box = self.box(name)
        if not box.running():
            return send({"type": "error", "message": f"{name} is not running (sosa start {name})"})
        self.touch(name)
        stack = self.stack_for(box)
        a = box.conf.get("agent") or stack.agent
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return send({"type": "error", "message": "turn needs text"})
        who = str(req.get("who") or "someone")[:80]
        role = str(req.get("role") or "guest").lower()
        if role not in ("owner", "guest", "admin"):
            role = "guest"
        channel = str(req.get("channel") or "api")[:40]
        facts = self.facts(box, stack)
        if facts:
            send({"type": "facts", "text": facts})
        header = f"[{channel} · from {who} · {role.upper()}]"
        prompt = header + ("\n" + facts if facts else "") + "\n\n" + text
        mreq = {"cmd": "turn", "prompt": prompt, "system": self.system_prompt(box, stack), "system_mode": "append",
                "meta": {"who": who, "role": role, "channel": channel, "box": name},
                "model": req.get("model") or a.get("model"), "max_turns": int(req.get("max_turns") or a.get("max_turns") or 50),
                "memory": a.get("memory", True), "tools": a.get("tools")}
        if req.get("effort") or a.get("effort"):
            mreq["effort"] = req.get("effort") or a.get("effort")
        if req.get("max_budget_usd") is not None or a.get("max_budget_usd") is not None:
            mreq["max_budget_usd"] = req.get("max_budget_usd") if req.get("max_budget_usd") is not None else a.get("max_budget_usd")
        t0 = time.time()
        final = None
        tools = []
        try:
            for ev in mindpy.request(box.target, mreq, timeout=float(a.get("timeout_s") or 1800) + 60):
                if ev.get("type") == "tool":
                    tools.append(ev)
                if ev.get("type") in ("done", "error"):
                    final = ev
                    break
                send(ev)
        except (OSError, ConnectionError, ValueError) as e:
            final = {"type": "error", "message": f"the mind went away: {e}"}
        if final is None:
            final = {"type": "error", "message": "the mind closed the connection mid-turn"}
        # the changes history, named for whose turn it was
        try:
            first = text.strip().splitlines()[0][:60]
            ev = box.exec(["/opt/sosa/bin/changes", "snapshot", f"{who} ({role}): {first}"], 60, user=a["user"], cwd="/")
            if (ev.get("stdout") or "").strip():
                final["changes"] = ev["stdout"].strip()
        except (OSError, ConnectionError, ValueError):
            pass
        rec = {"at": time.strftime("%FT%T"), "who": who, "role": role, "channel": channel, "text": text,
               "reply": final.get("text", ""), "type": final["type"], "cost_usd": final.get("cost_usd"),
               "tools": [t.get("name") + " " + t.get("input_summary", "") for t in tools], "seconds": round(time.time() - t0, 1),
               "session": final.get("session"), "changes": final.get("changes")}
        if final["type"] == "error":
            rec["error"] = final.get("message")
        with open(box.transcripts, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.log(f"{name}: turn from {who} ({role}): {final['type']}, {len(final.get('text', ''))} chars, "
                 f"{len(tools)} tools, ${final.get('cost_usd') or 0:.4f}, {rec['seconds']} s")
        send(final)

    # -- bake and check
    def bake(self, sname, send):
        sp = os.path.join(self.home.stacks, sname)
        stack = stackmod.load(sp)
        probs = stack.problems()
        if probs:
            raise BoxError("the stack has problems: " + "; ".join(probs))
        name = f"bake-{stack.name}"[:32]
        box = self.box(name)
        if box.exists():
            self.destroy(name)
        t0 = time.time()
        send({"line": f"bake {stack.name}: a fresh box from {os.path.basename(self.home.base_image)}"})
        self.create({"name": name, "stack": sname, "mem": stack.box.get("bake_mem") or stack.box["mem"], "cpus": stack.box["cpus"],
                     "disk": stack.box["disk"]})
        box.save_conf({**box.conf, "bake": True, "image": self.home.base_image})
        # from the plain image, not a previous bake
        box = self.box(name)
        try:
            self.start(name, wait=300)
            a = stack.agent
            send({"line": f"up after {time.time() - t0:.0f} s; the egress allows everything for the bake"})
            box.put_text("/etc/apt/apt.conf.d/90sosa-bake", 'Acquire::http::Proxy "http://127.0.0.1:3128";\nAcquire::https::Proxy "http://127.0.0.1:3128";\n')
            box.sh("printf 'HTTP_PROXY=http://127.0.0.1:3128\\nHTTPS_PROXY=http://127.0.0.1:3128\\nhttp_proxy=http://127.0.0.1:3128\\n"
                   "https_proxy=http://127.0.0.1:3128\\nNO_PROXY=localhost,127.0.0.1\\nno_proxy=localhost,127.0.0.1\\n' >> /etc/sosa/env")
            if stack.file("bake.sh"):
                send({"line": "bake.sh"})
                box.sh("rm -f /var/log/sosa-bake.rc; (cd /opt/sosa/stack && bash ./bake.sh > /var/log/sosa-bake.log 2>&1; "
                       "echo $? > /var/log/sosa-bake.rc) >/dev/null 2>&1 &", 30)
                seen = 0
                rc = None
                deadline = time.time() + 3 * 3600
                while time.time() < deadline:
                    time.sleep(2)
                    ev = box.exec(["bash", "-c", f"tail -c +{seen + 1} /var/log/sosa-bake.log; cat /var/log/sosa-bake.rc 2>/dev/null | sed 's/^/RC=/'"], 30)
                    out = ev.get("stdout") or ""
                    m = re.search(r"RC=(\d+)\s*$", out)
                    if m:
                        rc = int(m.group(1))
                        out = out[:m.start()]
                    if out:
                        seen += len(out.encode())
                        for line in out.rstrip("\n").split("\n"):
                            send({"line": "  " + line})
                    if rc is not None:
                        break
                if rc is None:
                    raise BoxError("bake.sh ran past 3 hours")
                if rc != 0:
                    raise BoxError(f"bake.sh exited {rc} (the box {name} is still up to look at: sosa exec {name} tail -50 /var/log/sosa-bake.log)")
                box.sh(f"chown -R {a['user']}:{a['user']} {stack.app['dir']} 2>/dev/null; true")
            if stack.has_app:
                send({"line": "proving the app: app load (build, start, health)"})
                ev = box.exec(["/opt/sosa/bin/sosa-app", "load"], 1800)
                for line in (ev.get("stdout") or "").rstrip().split("\n"):
                    send({"line": "  " + line})
                if ev.get("rc") != 0:
                    raise BoxError(f"app load failed in the bake (rc {ev.get('rc')}): {(ev.get('stderr') or '')[-300:]}")
                time.sleep(2)
                box.exec(["/opt/sosa/bin/sosa-app", "stop"], 60)
            send({"line": "what makes a box someone's, out"})
            box.sh(f"rm -rf /home/{a['user']}/.claude /home/{a['user']}/.claude.json* /home/{a['user']}/.bash_history "
                   f"/var/lib/sosa/session /var/lib/sosa/app /etc/sosa/token /srv/changes.git /srv/changes.paths "
                   f"/var/log/sosa-bake.* /etc/apt/apt.conf.d/90sosa-bake /root/.bash_history; "
                   f"sed -i -E '/^(HTTPS?_PROXY|https?_proxy|NO_PROXY|no_proxy)=/d' /etc/sosa/env; "
                   f"journalctl --rotate --vacuum-time=1s >/dev/null 2>&1; cloud-init clean --logs >/dev/null 2>&1; sync", 120)
            claude_v = (box.health() or {}).get("claude")
            send({"line": "powering off"})
            r = self.running.get(name)
            if r:
                r.stopping = True
            box.sh("systemctl poweroff", 10) if False else box.exec(["systemctl", "--no-block", "poweroff"], 10)
            for _ in range(180):
                if not box.running():
                    break
                time.sleep(1)
            else:
                raise BoxError("the bake box didn't power off")
            out = os.path.join(self.home.images, f"{stack.name}.raw")
            subprocess.run(["cp", "--sparse=always", box.disk, out + ".part"], check=True)
            os.replace(out + ".part", out)
            manifest = {"stack": stack.name, "created": time.strftime("%FT%T"), "claude": claude_v,
                        "base": os.path.basename(self.home.base_image), "seconds": round(time.time() - t0),
                        "bytes": os.stat(out).st_blocks * 512}
            with open(out[:-4] + ".json", "w") as f:
                json.dump(manifest, f, indent=2)
            send({"line": f"image {out} ({manifest['bytes'] >> 20} MB on disk) in {manifest['seconds']} s"})
            self.destroy(name)
            return manifest
        except Exception:
            send({"line": f"the bake box {name} is left up to look at (sosa exec {name} ...; sosa destroy {name})"})
            raise

    def check(self, sname, quick, send):
        sp = os.path.join(self.home.stacks, sname)
        stack = stackmod.load(sp)
        probs = stack.problems()
        send({"line": f"check {stack.name}: {len(probs)} problem(s) in the files" + (": " + "; ".join(probs) if probs else "")})
        if probs or quick:
            return {"done": not probs, "problems": probs}
        self.bake(sname, send)
        name = f"check-{stack.name}"[:32]
        if self.box(name).exists():
            self.destroy(name)
        try:
            send({"line": f"a box from the image: {name}"})
            self.create({"name": name, "stack": sname, "start": True})
            box = self.box(name)
            st = self.status(name)
            if stack.has_app:
                send({"line": "app load in the new box"})
                ev = box.exec(["/opt/sosa/bin/sosa-app", "load"], 1800)
                if ev.get("rc") != 0:
                    raise BoxError("app load failed in the new box: " + (ev.get("stdout") or "")[-500:])
                st = self.status(name)
                if not (st.get("app") or {}).get("healthy"):
                    raise BoxError(f"the app isn't healthy after load: {st.get('app')}")
                send({"line": f"healthy behind the stable port; preview {st.get('preview')}"})
            send({"line": "one turn"})
            got = {}
            self.turn(name, {"text": "Say what box you're in and what's in the app dir, in two lines. Don't change anything.",
                             "who": "check", "role": "owner", "channel": "check"}, lambda ev: got.update({ev["type"]: ev}))
            if "done" not in got:
                raise BoxError(f"the turn failed: {got.get('error', {}).get('message')}")
            send({"line": "  " + (got["done"].get("text") or "").strip().replace("\n", "\n  ")})
            self.destroy(name)
            return {"done": True, "problems": []}
        except Exception as e:  # noqa: BLE001
            send({"line": f"failed: {e} (the box {name} is left up to look at)"})
            return {"done": False, "problems": [str(e)]}

    # -- relay
    def start_relay(self):
        key = os.path.join(self.home.path, "anthropic-key")
        if not os.path.exists(key):
            self.log(f"no relay: {key} doesn't exist (boxes can use a token instead)")
            return
        relaymod.CFG.boxes = self.home.boxes
        relaymod.CFG.usage = os.path.join(self.home.path, "usage.jsonl")
        relaymod.CFG.key = open(key).read().strip()
        wf = os.path.join(self.home.path, "workspace")
        if os.path.exists(wf):
            w = open(wf).read().strip()
            relaymod.CFG.workspace = w.partition("=")[2] or w
        relaymod.CFG.box_day = float(os.environ.get("SOSA_BOX_DAY_USD") or 20)
        relaymod.CFG.day = float(os.environ.get("SOSA_DAY_USD") or 100)
        relaymod.load_spent()
        threading.Thread(target=relaymod.watch, daemon=True).start()
        self.relay_on = True
        self.log(f"relay on: ${relaymod.CFG.box_day:g} a box a day, ${relaymod.CFG.day:g} for all")

    def usage(self, days):
        by = {}
        p = os.path.join(self.home.path, "usage.jsonl")
        since = time.strftime("%F", time.localtime(time.time() - 86400 * (days - 1)))
        try:
            for line in open(p):
                u = json.loads(line)
                if u["day"] >= since:
                    by.setdefault(u["day"], {}).setdefault(u["box"], 0.0)
                    by[u["day"]][u["box"]] += u.get("cost_usd") or 0
        except FileNotFoundError:
            pass
        return {"days": by, "relay": self.relay_on}

    def shutdown(self):
        for n in list(self.running):
            self.log(f"{n}: stopping (daemon shutdown; it comes back with the daemon)")
            self.stop(n, wanted_down=False)


# ---------------------------------------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    d: Daemon = None
    api_token = None

    def log_message(self, *a):
        pass

    def address_string(self):
        return "client"

    def body(self):
        n = int(self.headers.get("content-length") or 0)
        if n > (4 << 20):
            raise BoxError("request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
        except ValueError:
            raise BoxError("the body is not JSON")
        return obj if isinstance(obj, dict) else {}

    def send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream(self, fn):
        """NDJSON, chunked. fn(send) does the work; its return value (or an error) ends the stream."""
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        mu = threading.Lock()

        def send(obj):
            data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
            with mu:
                try:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                except OSError:
                    pass
        try:
            res = fn(send)
            if res is not None:
                send({"done": res} if not isinstance(res, dict) or "done" not in res else res)
        except Exception as e:  # noqa: BLE001
            send({"type": "error", "message": str(e), "done": False})
            self.d.log(f"stream error: {type(e).__name__}: {e}")
        with mu:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
        self.close_connection = True

    def tunnel(self, box, b):
        """After `HTTP/1.1 200 OK\r\n\r\n`, this connection is the box's port (front.py's way in)."""
        d = self.d
        ports = box.conf.get("ports") or []
        if not ports:
            return self.send_json(400, {"error": f"{box.name} has no app ports"})
        port = int(b.get("port") or ports[0])
        if port not in ports:
            return self.send_json(400, {"error": f"{box.name}'s ports are {ports}"})
        if not box.running():
            if not b.get("wake", True) or box.conf.get("bake"):
                return self.send_json(409, {"error": f"{box.name} is not running"})
            d.log(f"{box.name}: waking for a tunnel to port {port}")
            d.start(box.name)
        d.touch(box.name)
        self.wfile.write(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True
        d.preview_bridge(box, self.connection, port)

    def authorized(self):
        if self.api_token is None:
            return True
        return self.headers.get("authorization") == f"Bearer {self.api_token}"

    def route(self, method):
        if not self.authorized():
            return self.send_json(401, {"error": "a bearer token is needed"})
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        d = self.d
        try:
            if parts[:1] != ["v1"]:
                return self.send_json(404, {"error": "not found"})
            p = parts[1:]
            if p == ["health"] and method == "GET":
                return self.send_json(200, {"ok": True, "boxes_up": len(d.running), "relay": d.relay_on, "home": d.home.path,
                                            "pid": os.getpid()})
            if p == ["stacks"]:
                if method == "GET":
                    return self.send_json(200, {"stacks": d.stacks()})
                if method == "POST":
                    return self.send_json(200, d.add_stack(os.path.expanduser(self.body().get("path") or "")))
            if len(p) == 2 and p[0] == "stacks" and method == "GET":
                sp = os.path.join(d.home.stacks, p[1])
                return self.send_json(200, stackmod.load(sp).describe())
            if p == ["images"]:
                if method == "GET":
                    return self.send_json(200, {"images": d.images()})
                if method == "POST":
                    b = self.body()
                    return self.stream(lambda send: d.bake(b.get("stack") or "", send))
            if p == ["check"] and method == "POST":
                b = self.body()
                return self.stream(lambda send: d.check(b.get("stack") or "", bool(b.get("quick")), send))
            if p == ["usage"] and method == "GET":
                return self.send_json(200, d.usage(int(q.get("days", ["7"])[0])))
            if p == ["boxes"]:
                if method == "GET":
                    return self.send_json(200, {"boxes": d.list_boxes()})
                if method == "POST":
                    return self.send_json(200, d.create(self.body()))
            if len(p) >= 2 and p[0] == "boxes":
                name = p[1]
                box = d.box(name)
                if not box.exists():
                    return self.send_json(404, {"error": f"no box {name!r}"})
                action = p[2] if len(p) > 2 else None
                if action is None:
                    if method == "GET":
                        return self.send_json(200, d.status(name))
                    if method == "DELETE":
                        return self.send_json(200, d.destroy(name))
                b = self.body() if method in ("POST", "PUT") else {}
                if action == "start" and method == "POST":
                    return self.send_json(200, d.start(name))
                if action == "stop" and method == "POST":
                    return self.send_json(200, d.stop(name, int(b.get("timeout") or 120)))
                if action == "idle" and method == "POST":
                    m = int(b.get("minutes") or 0)
                    box.save_conf({k: v for k, v in {**box.conf, "idle": m}.items() if m or k != "idle"})
                    d.touch(name)
                    return self.send_json(200, {"name": name, "idle": m})
                if action == "tunnel" and method == "POST":
                    return self.tunnel(box, b)
                if action == "turn" and method == "POST":
                    return self.stream(lambda send: d.turn(name, b, send))
                if action == "cancel" and method == "POST":
                    return self.send_json(200, mindpy.one(box.target, {"cmd": "cancel"}, 30))
                if action == "new" and method == "POST":
                    return self.send_json(200, mindpy.one(box.target, {"cmd": "new"}, 30))
                if action == "exec" and method == "POST":
                    argv = b.get("argv")
                    if not isinstance(argv, list) or not argv:
                        return self.send_json(400, {"error": "exec needs argv"})
                    d.touch(name)
                    return self.send_json(200, box.exec(argv, int(b.get("timeout") or 120), b.get("user"), b.get("cwd"),
                                                        b.get("stdin"), b.get("env")))
                if action == "push" and method == "POST":
                    st = d.stack_for(box) if b.get("stack", True) else None
                    return self.send_json(200, {"name": name, "done": box.push(st, bool(b.get("claude")))})
                if action == "token" and method == "POST":
                    tok = (b.get("token") or "").strip()
                    if not tok:
                        return self.send_json(400, {"error": "token needed"})
                    with open(os.open(box.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                        f.write(tok + "\n")
                    c = box.conf
                    if c.get("auth") != "token":
                        box.save_conf({**c, "auth": "token"})
                    if box.running():
                        box.set_token(tok)
                    return self.send_json(200, {"name": name, "stored": True, "sent": box.running()})
                if action == "snapshot" and method == "POST":
                    return self.send_json(200, {"name": name, "snapshot": box.snapshot(b.get("label") or "")})
                if action == "snapshots" and method == "GET":
                    return self.send_json(200, {"name": name, "snapshots": box.list_snapshots()})
                if action == "rollback" and method == "POST":
                    was = box.running()
                    if was:
                        d.stop(name, wanted_down=False)
                    box.rollback_disk(b.get("snapshot") or "")
                    if was:
                        d.start(name)
                    return self.send_json(200, {"name": name, "rolled_back_to": b.get("snapshot"), "running": was})
                if action == "prompt":
                    if method == "GET":
                        t = open(box.prompt_file).read() if os.path.exists(box.prompt_file) else ""
                        return self.send_json(200, {"name": name, "prompt": t})
                    if method == "PUT":
                        with open(box.prompt_file, "w") as f:
                            f.write(b.get("prompt") or "")
                        return self.send_json(200, {"name": name, "prompt": b.get("prompt") or ""})
                if action == "facts" and method == "GET":
                    return self.send_json(200, {"name": name, "facts": d.facts(box, d.stack_for(box))})
                if action == "transcript" and method == "GET":
                    n = int(q.get("n", ["20"])[0])
                    rows = []
                    try:
                        rows = [json.loads(x) for x in open(box.transcripts).read().splitlines()[-n:]]
                    except FileNotFoundError:
                        pass
                    return self.send_json(200, {"name": name, "turns": rows})
                if action == "serial" and method == "GET":
                    n = int(q.get("n", ["50"])[0])
                    try:
                        lines = open(box.serial, errors="replace").read().splitlines()[-n:]
                    except FileNotFoundError:
                        lines = []
                    return self.send_json(200, {"name": name, "lines": lines})
            return self.send_json(404, {"error": f"no route {method} {url.path}"})
        except (BoxError, stackmod.StackError) as e:
            return self.send_json(400, {"error": str(e)})
        except (OSError, ConnectionError, ValueError) as e:
            return self.send_json(502, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:  # noqa: BLE001
            d.log(f"error on {method} {self.path}: {type(e).__name__}: {e}")
            return self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

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


class TcpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=os.environ.get("SOSA_HOME") or "~/.sosa")
    ap.add_argument("--tcp", help="also listen on host:port, with a bearer token (<home>/api-token); "
                                  "or put the address in <home>/tcp")
    a = ap.parse_args()
    home = Home(a.home)
    home.ensure()
    # one daemon per home: a second one (the CLI starting one while another is starting) leaves at once
    import fcntl
    lockf = open(os.path.join(home.path, "sosad.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("sosad: another daemon holds this home", file=sys.stderr)
        sys.exit(0)
    d = Daemon(home)
    Handler.d = d
    sock = os.path.join(home.path, "sosad.sock")
    with open(os.path.join(home.path, "sosad.pid"), "w") as f:
        f.write(str(os.getpid()))
    if not a.tcp and os.path.exists(os.path.join(home.path, "tcp")):
        a.tcp = open(os.path.join(home.path, "tcp")).read().strip() or None
    d.start_relay()
    srv = UnixServer(sock, Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d.log(f"sosad listening on {sock}")
    threading.Thread(target=d.start_wanted, daemon=True).start()
    if a.tcp:
        host, _, port = a.tcp.rpartition(":")
        tp = os.path.join(home.path, "api-token")
        if not os.path.exists(tp):
            import secrets
            with open(os.open(tp, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
        TcpHandler = type("TcpHandler", (Handler,), {"api_token": open(tp).read().strip()})
        t = TcpServer((host or "127.0.0.1", int(port)), TcpHandler)
        threading.Thread(target=t.serve_forever, daemon=True).start()
        d.log(f"sosad also on {a.tcp} (bearer token in {tp})")
    import signal
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        stop.wait(1)
    d.log("sosad: shutting down")
    d.shutdown()
    srv.server_close()
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
