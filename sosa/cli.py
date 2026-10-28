#!/usr/bin/env python3
"""sosa: an agent in a box. The CLI is a thin client of sosad (sosa/daemon.py), which it starts if it isn't up.

  boxes
    sosa create NAME --stack S [--mem 2G --cpus 2 --disk 10G --auth relay|token --owner WHO --idle MIN] [--start]
    sosa start|stop|restart NAME          start waits for the mind (first boot ~30 s); stop is the power button
    sosa idle NAME MINUTES                untouched for that long, the box is stopped and marked asleep (0: never);
                                          a start, or a request at the front door, wakes it
    sosa list                             every box; `sosa status NAME` for one, with the mind, app and preview
    sosa ask NAME "text" [--who W --role owner|guest --channel C --model M --effort E]   one turn, streamed
    sosa chat NAME [--who W --role R]     a conversation in the terminal (Ctrl-D ends it)
    sosa exec NAME -- CMD...              a command in the box as root (--user U, --cwd D)
    sosa facts NAME                       what the stack's facts.sh says now
    sosa prompt NAME [--set-file F | --clear]   the owner's prompt, sent with every turn
    sosa new NAME | sosa cancel NAME      forget the conversation | kill the turn under way
    sosa snapshot NAME [LABEL] | sosa snapshots NAME | sosa rollback NAME SNAPSHOT
    sosa push NAME [--claude] [--no-stack]   ship sosa's guest programs (and the stack) into a running box
    sosa token NAME                       paste a `claude setup-token` token (hidden) for a box on token auth
    sosa transcript NAME [-n 20] | sosa console NAME [-n 50] | sosa preview NAME
    sosa destroy NAME [-f]                stop and delete it (asks first)
  stacks and images
    sosa stack init DIR [--name N]        a stack skeleton to fill in (docs/CONTRACT.md)
    sosa stack add DIR                    copy a stack into ~/.sosa/stacks and check its files
    sosa stack list | sosa stack show S
    sosa stack check S [--quick]          the contract test: bake, a box from the image, app load, one turn
    sosa image S | sosa images            bake a stack's image (what its boxes start from)
  the host
    sosa install                          hypervisor, base image, dirs (install.sh)
    sosa key                              paste the Claude API key the relay holds (hidden); boxes then default to relay auth
    sosa daemon [--tcp 127.0.0.1:8790]    run sosad in the foreground (an address in ~/.sosa/tcp makes every start listen there)
    sosa front [--listen 127.0.0.1:8180 --zone localhost]   the front door: http://NAME.ZONE/ is box NAME's app, woken
                                          on request; hosts and a login secret in ~/.sosa/front.json (sosa/front.py)
    sosa usage [DAYS]                     what the relay counted
  --json on any command prints the daemon's answer as it came. SOSA_HOME (default ~/.sosa).
"""
import argparse
import getpass
import http.client
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME = os.path.abspath(os.path.expanduser(os.environ.get("SOSA_HOME") or "~/.sosa"))
SOCK = os.path.join(HOME, "sosad.sock")
PY = "/usr/bin/python3"


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("sosad", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def die(msg, rc=1):
    print(f"sosa: {msg}", file=sys.stderr)
    sys.exit(rc)


def daemon_up():
    try:
        c = UnixConn(SOCK, 3)
        c.request("GET", "/v1/health")
        r = c.getresponse()
        ok = r.status == 200
        r.read()
        c.close()
        return ok
    except OSError:
        return False


def ensure_daemon():
    if daemon_up():
        return
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    log = open(os.path.join(HOME, "sosad.out"), "ab")
    subprocess.Popen([PY, "-I", os.path.join(HERE, "daemon.py"), "--home", HOME], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        time.sleep(0.2)
        if daemon_up():
            print("(started sosad)", file=sys.stderr)
            return
    die(f"sosad didn't come up; see {HOME}/sosad.out")


def call(method, path, body=None, timeout=3600):
    ensure_daemon()
    c = UnixConn(SOCK, timeout)
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data, headers={"content-type": "application/json"} if data else {})
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        obj = json.loads(raw)
    except ValueError:
        die(f"{method} {path}: HTTP {r.status}: {raw[:300]!r}")
    if r.status != 200:
        die(obj.get("error") or f"HTTP {r.status}")
    return obj


def stream(method, path, body=None, timeout=4 * 3600):
    """Yields each NDJSON event."""
    ensure_daemon()
    c = UnixConn(SOCK, timeout)
    data = json.dumps(body or {}).encode()
    c.request(method, path, body=data, headers={"content-type": "application/json"})
    r = c.getresponse()
    if r.status != 200:
        raw = r.read()
        try:
            die(json.loads(raw).get("error"))
        except ValueError:
            die(f"HTTP {r.status}: {raw[:300]!r}")
    buf = b""
    while True:
        chunk = r.read1(65536)
        if not chunk:
            break
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for line in lines:
            if line.strip():
                yield json.loads(line)
    c.close()


def out(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return True
    return False


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} PB"


def show_turn(events, dim):
    for ev in events:
        t = ev.get("type")
        if t == "facts":
            continue
        if t == "delta":
            sys.stdout.write(ev["text"])
            sys.stdout.flush()
        elif t == "tool":
            print(dim(f"\n[{ev['name']} {ev.get('input_summary', '')}]"), file=sys.stderr, flush=True)
        elif t == "done":
            print(flush=True)
            extra = f", {ev['note']}" if ev.get("note") else ""
            print(dim(f"(${ev.get('cost_usd') or 0:.4f}, {(ev.get('duration_ms') or 0) / 1000:.0f} s, "
                      f"{ev.get('tools', 0)} tools{', ' + ev['changes'] if ev.get('changes') else ''}{extra})"), file=sys.stderr)
            return 0
        elif t == "error":
            if ev.get("text"):
                print(ev["text"])
            print(f"error: {ev.get('message')}", file=sys.stderr)
            return 1
    print("\nerror: the stream ended without a reply", file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sosa", add_help=False)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    ap.add_argument("cmd", nargs="?")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    J = a.json
    tty = sys.stderr.isatty()
    dim = (lambda s: f"\x1b[2m{s}\x1b[0m") if tty else (lambda s: s)
    cmd, rest = a.cmd, a.rest
    if a.help or not cmd or cmd in ("help", "-h", "--help"):
        print(__doc__.strip())
        return 0

    def sub(spec, args):
        p = argparse.ArgumentParser(prog=f"sosa {cmd}")
        for s in spec:
            p.add_argument(*s[0], **s[1])
        return p.parse_args(args)

    if cmd == "daemon":
        os.execv(PY, [PY, "-I", os.path.join(HERE, "daemon.py"), "--home", HOME] + rest)
    if cmd == "front":
        ensure_daemon()
        os.execv(PY, [PY, "-I", os.path.join(HERE, "front.py"), "--home", HOME] + rest)
    if cmd == "install":
        os.execv("/bin/bash", ["bash", os.path.join(REPO, "install.sh")] + rest)
    if cmd == "key":
        os.makedirs(HOME, mode=0o700, exist_ok=True)
        if not sys.stdin.isatty():
            die("run this in a terminal: it asks for the key with hidden input")
        k = getpass.getpass("Claude API key or setup-token token (hidden): ").strip()
        if not k.startswith("sk-ant-"):
            die("that doesn't look like one")
        p = os.path.join(HOME, "anthropic-key")
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(k + "\n")
        print(f"kept in {p} (0600). Restart sosad to pick it up: kill $(cat {HOME}/sosad.pid); the next sosa command starts it.")
        return 0
    if cmd == "usage":
        days = int(rest[0]) if rest and rest[0].isdigit() else 7
        u = call("GET", f"/v1/usage?days={days}")
        if out(u, J):
            return 0
        if not u["days"]:
            print("nothing counted" + ("" if u["relay"] else " (no relay: boxes on token auth aren't metered here)"))
        for day, by in sorted(u["days"].items()):
            print(day, f"${sum(by.values()):.2f}", "  ".join(f"{k} ${v:.2f}" for k, v in sorted(by.items())))
        return 0

    # -- stacks
    if cmd == "stack":
        if not rest:
            die("stack init|add|list|show|check")
        sc, sr = rest[0], rest[1:]
        if sc == "init":
            o = sub([(("dir",), {}), (("--name",), {})], sr)
            sys.path.insert(0, REPO)
            from sosa import stack as stackmod
            name = o.name or os.path.basename(os.path.abspath(o.dir))
            try:
                stackmod.init(o.dir, name)
            except stackmod.StackError as e:
                die(str(e))
            print(f"a stack skeleton in {o.dir}: fill in stack.toml, brief.md, seed/, tools/; then sosa stack add {o.dir}")
            return 0
        if sc == "add":
            o = sub([(("dir",), {})], sr)
            d = call("POST", "/v1/stacks", {"path": os.path.abspath(o.dir)})
            if out(d, J):
                return 0
            print(f"stack {d['name']} added ({d['path']}): {', '.join(d['files'])}" + (f"; tools: {', '.join(d['tools'])}" if d["tools"] else ""))
            print(f"next: sosa stack check {d['name']}   (or sosa image {d['name']}, then sosa create NAME --stack {d['name']})")
            return 0
        if sc == "list":
            d = call("GET", "/v1/stacks")
            if out(d, J):
                return 0
            for s in d["stacks"]:
                print(f"{s['name']:<16} {'image' if s.get('image') else 'no image':<9} {'app' if s.get('has_app') else 'no app':<7} "
                      f"{s.get('description', '')}" + (f"  PROBLEMS: {'; '.join(s['problems'])}" if s.get("problems") else ""))
            if not d["stacks"]:
                print("no stacks (sosa stack add DIR)")
            return 0
        if sc == "show":
            o = sub([(("name",), {})], sr)
            d = call("GET", f"/v1/stacks/{o.name}")
            if out(d, J):
                return 0
            print(json.dumps(d, indent=2))
            return 0
        if sc == "check":
            o = sub([(("name",), {}), (("--quick",), {"action": "store_true"})], sr)
            res = None
            for ev in stream("POST", "/v1/check", {"stack": o.name, "quick": o.quick}):
                if "line" in ev:
                    print(ev["line"], flush=True)
                if "done" in ev:
                    res = ev
            if res is None:
                die("the check ended without a verdict")
            if J:
                print(json.dumps(res))
            print("check: " + ("passed" if res.get("done") else "FAILED: " + "; ".join(res.get("problems") or [res.get("message", "")])))
            return 0 if res.get("done") else 1
        die(f"stack {sc}?")
    if cmd == "image":
        o = sub([(("stack",), {})], rest)
        res = None
        for ev in stream("POST", "/v1/images", {"stack": o.stack}):
            if "line" in ev:
                print(ev["line"], flush=True)
            if ev.get("type") == "error":
                die(ev.get("message"))
            if "done" in ev:
                res = ev["done"]
        if J and res:
            print(json.dumps(res))
        return 0
    if cmd == "images":
        d = call("GET", "/v1/images")
        if out(d, J):
            return 0
        for i in d["images"]:
            print(f"{i['stack']:<16} {human(i['bytes']):>8}  {i.get('created', '')}  claude {i.get('claude', '?')}")
        if not d["images"]:
            print("no images (sosa image STACK)")
        return 0

    # -- boxes
    if cmd in ("list", "ls"):
        d = call("GET", "/v1/boxes")
        if out(d, J):
            return 0
        for b in d["boxes"]:
            state = "running" if b["running"] else ("asleep" if b.get("asleep") else "down")
            print(f"{b['name']:<16} {state:<8} {b['stack']:<12} {b['auth']:<6} "
                  f"{b.get('owner') or ''}{'  (bake)' if b.get('bake') else ''}{'  idle ' + str(b['idle']) + 'm' if b.get('idle') else ''}")
        if not d["boxes"]:
            print("no boxes (sosa create NAME --stack S)")
        return 0
    if cmd == "create":
        o = sub([(("name",), {}), (("--stack",), {"required": True}), (("--mem",), {}), (("--cpus",), {"type": int}),
                 (("--disk",), {}), (("--auth",), {"choices": ["relay", "token"]}), (("--owner",), {}),
                 (("--idle",), {"type": int, "help": "minutes untouched before it sleeps"}),
                 (("--start",), {"action": "store_true"})], rest)
        body = {k: v for k, v in vars(o).items() if v is not None and v is not False}
        d = call("POST", "/v1/boxes", body)
        if out(d, J):
            return 0
        print(f"created {d['name']} from {d['stack']} ({d['mem']}, {d['cpus']} vcpu, {d['disk']}, auth {d['auth']})"
              + (": running" if d["running"] else f"\nnext: sosa start {d['name']}"))
        if d.get("auth") == "token":
            print(f"      it needs a token: sosa token {d['name']}  (or `sosa key` for the relay, before creating boxes)")
        return 0
    if not rest and cmd not in ("list", "ls"):
        die(f"{cmd} NAME (sosa help)")
    name = rest[0]
    args = rest[1:]
    if cmd == "status":
        d = call("GET", f"/v1/boxes/{name}")
        if out(d, J):
            return 0
        du = d.get("disk_usage") or {}
        state = "running since " + d["since"] if d["running"] else ("asleep since " + d["asleep"] if d.get("asleep") else "down")
        if d.get("idle"):
            state += f"; sleeps after {d['idle']} min idle" + (f" (idle {d['idle_for'] // 60} min now)" if "idle_for" in d else "")
        print(f"{d['name']}: {state}; stack {d['stack']}, {d['mem']}, "
              f"{d['cpus']} vcpu, disk {d['disk']} ({human(du.get('used', 0))} used); auth {d['auth']}; "
              f"{d['snapshots']} snapshot(s); egress {d['egress']['allowed']} allowed, {d['egress']['refused']} refused")
        if d["egress"].get("last_refused"):
            print(f"  last refused: {d['egress']['last_refused']}")
        m = d.get("mind")
        if d["running"]:
            if m:
                print(f"  mind: claude {m.get('claude')}, auth {m.get('auth') or 'NONE'}, session {m.get('session') or 'none yet'}"
                      f"{', busy' if m.get('busy') else ''}{', ' + str(m.get('queued')) + ' queued' if m.get('queued') else ''}")
            else:
                print("  mind: not answering (booting? sosa console NAME)")
            app = d.get("app")
            if app:
                if app.get("error"):
                    print(f"  app: {app['error']}")
                elif app.get("live"):
                    lv = app["live"]
                    print(f"  app: live slot {lv['slot']} on {lv['port']} since {lv['since']}, "
                          f"{'healthy' if app.get('healthy') else 'NOT healthy'}, stable port {app['stable_port']}, last good {app.get('good') or 'none'}")
                else:
                    print(f"  app: nothing live yet (sosa exec {name} -- app load); stable port {app.get('stable_port')}")
            if d.get("preview"):
                print("  preview: " + ", ".join(f"{u} -> box port {p}" for p, u in d["preview"].items()))
        return 0
    if cmd == "start":
        d = call("POST", f"/v1/boxes/{name}/start")
        if out(d, J):
            return 0
        print(f"{name} is up" + (f"; preview {', '.join(d['preview'].values())}" if d.get("preview") else ""))
        return 0
    if cmd == "stop":
        d = call("POST", f"/v1/boxes/{name}/stop")
        if out(d, J):
            return 0
        print(f"{name}: {d['result']}")
        return 0
    if cmd == "idle":
        if not args or not args[0].isdigit():
            die(f"idle {name} MINUTES (0: never)")
        d = call("POST", f"/v1/boxes/{name}/idle", {"minutes": int(args[0])})
        if out(d, J):
            return 0
        print(f"{name}: " + (f"sleeps after {d['idle']} min untouched" if d["idle"] else "never sleeps"))
        return 0
    if cmd == "restart":
        call("POST", f"/v1/boxes/{name}/stop")
        d = call("POST", f"/v1/boxes/{name}/start")
        print(f"{name} is up again")
        return 0
    if cmd == "ask":
        o = sub([(("text",), {"nargs": "+"}), (("--who",), {"default": getpass.getuser()}), (("--role",), {"default": "owner"}),
                 (("--channel",), {"default": "cli"}), (("--model",), {}), (("--effort",), {})], args)
        body = {"text": " ".join(o.text), "who": o.who, "role": o.role, "channel": o.channel}
        if o.model:
            body["model"] = o.model
        if o.effort:
            body["effort"] = o.effort
        if J:
            for ev in stream("POST", f"/v1/boxes/{name}/turn", body):
                print(json.dumps(ev, ensure_ascii=False), flush=True)
            return 0
        return show_turn(stream("POST", f"/v1/boxes/{name}/turn", body), dim)
    if cmd == "chat":
        o = sub([(("--who",), {"default": getpass.getuser()}), (("--role",), {"default": "owner"})], args)
        print(dim(f"chatting with {name} as {o.who} ({o.role}); Ctrl-D to leave"), file=sys.stderr)
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line.strip():
                continue
            show_turn(stream("POST", f"/v1/boxes/{name}/turn", {"text": line, "who": o.who, "role": o.role, "channel": "cli"}), dim)
    if cmd == "exec":
        o = sub([(("--user",), {}), (("--cwd",), {}), (("--timeout",), {"type": int, "default": 600}),
                 (("argv",), {"nargs": argparse.REMAINDER})], args)
        argv = o.argv[1:] if o.argv[:1] == ["--"] else o.argv
        if not argv:
            die("exec NAME -- CMD...")
        d = call("POST", f"/v1/boxes/{name}/exec", {"argv": argv, "user": o.user, "cwd": o.cwd, "timeout": o.timeout})
        if out(d, J):
            return 0
        sys.stdout.write(d.get("stdout", ""))
        sys.stderr.write(d.get("stderr", ""))
        return d.get("rc", 1)
    if cmd == "facts":
        d = call("GET", f"/v1/boxes/{name}/facts")
        print(d["facts"] if not J else json.dumps(d))
        return 0
    if cmd == "prompt":
        o = sub([(("--set-file",), {}), (("--clear",), {"action": "store_true"})], args)
        if o.set_file:
            d = call("PUT", f"/v1/boxes/{name}/prompt", {"prompt": open(o.set_file).read()})
            print(f"prompt set ({len(d['prompt'])} chars)")
        elif o.clear:
            call("PUT", f"/v1/boxes/{name}/prompt", {"prompt": ""})
            print("prompt cleared")
        else:
            d = call("GET", f"/v1/boxes/{name}/prompt")
            print(d["prompt"] if not J else json.dumps(d))
        return 0
    if cmd in ("new", "cancel"):
        d = call("POST", f"/v1/boxes/{name}/{cmd}")
        print(json.dumps(d) if J else ("the next turn starts a fresh conversation" if cmd == "new" else
                                       ("killed the turn" if d.get("killed") else "no turn was running")))
        return 0
    if cmd == "snapshot":
        d = call("POST", f"/v1/boxes/{name}/snapshot", {"label": args[0] if args else ""})
        print(json.dumps(d) if J else f"snapshot {d['snapshot']}")
        return 0
    if cmd == "snapshots":
        d = call("GET", f"/v1/boxes/{name}/snapshots")
        if out(d, J):
            return 0
        for s in d["snapshots"]:
            print(f"{s['name']:<44} {human(s['bytes'])}")
        if not d["snapshots"]:
            print("none")
        return 0
    if cmd == "rollback":
        if not args:
            die("rollback NAME SNAPSHOT (sosa snapshots NAME)")
        d = call("POST", f"/v1/boxes/{name}/rollback", {"snapshot": args[0]})
        print(json.dumps(d) if J else f"{name} rolled back to {d['rolled_back_to']}" + (", booting" if d["running"] else ""))
        return 0
    if cmd == "push":
        o = sub([(("--claude",), {"action": "store_true"}), (("--no-stack",), {"action": "store_true"})], args)
        d = call("POST", f"/v1/boxes/{name}/push", {"claude": o.claude, "stack": not o.no_stack})
        print(json.dumps(d) if J else "\n".join(d["done"]))
        return 0
    if cmd == "token":
        if not sys.stdin.isatty():
            die("run this in a terminal: it asks for the token with hidden input")
        print("Paste the token that `claude setup-token` printed (input is hidden).")
        tok = getpass.getpass("token: ").strip()
        if not tok:
            die("no token given")
        d = call("POST", f"/v1/boxes/{name}/token", {"token": tok})
        print(f"stored for {name}" + (" and sent in" if d.get("sent") else " (sent in at the next start)"))
        return 0
    if cmd == "transcript":
        o = sub([(("-n",), {"type": int, "default": 20})], args)
        d = call("GET", f"/v1/boxes/{name}/transcript?n={o.n}")
        if out(d, J):
            return 0
        for t in d["turns"]:
            print(f"--- {t['at']} {t['who']} ({t['role']}, {t['channel']}) {t['type']} ${t.get('cost_usd') or 0:.3f} {t['seconds']} s")
            print("> " + t["text"].replace("\n", "\n> "))
            print(t.get("reply") or t.get("error") or "")
        return 0
    if cmd == "console":
        o = sub([(("-n",), {"type": int, "default": 50})], args)
        d = call("GET", f"/v1/boxes/{name}/serial?n={o.n}")
        print("\n".join(d["lines"]) if not J else json.dumps(d))
        return 0
    if cmd == "preview":
        d = call("GET", f"/v1/boxes/{name}")
        if out(d.get("preview") or {}, J):
            return 0
        if not d.get("preview"):
            print("no preview: the box isn't running, or its stack has no app ports")
            return 1
        for p, u in d["preview"].items():
            print(f"{u}  -> box port {p}")
        return 0
    if cmd == "destroy":
        if "-f" not in args and sys.stdin.isatty():
            ok = input(f"Delete box {name}, its disk (the agent's memory) and its snapshots? [y/N] ")
            if ok.strip().lower() != "y":
                return 1
        d = call("DELETE", f"/v1/boxes/{name}")
        print(json.dumps(d) if J else f"destroyed {name}")
        return 0
    die(f"unknown command {cmd!r} (sosa help)")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
