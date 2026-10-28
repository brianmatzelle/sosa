#!/usr/bin/env python3
"""mind.py: the agent's mind, inside the box. Python stdlib only, one file.

It runs turns with `claude -p` and streams them back. One JSON request line per connection; NDJSON back;
then the connection closes. The host reaches it over vsock (cloud-hypervisor's hybrid vsock); nothing else can.

Serve (sosa-mind.service, as root; claude runs as the agent user in the app dir):
    mind.py serve --guest --listen vsock:5000 --home /home/sosa --user sosa --cwd /srv/app \
        --state /var/lib/sosa --token-file /etc/sosa/token --proxy http://127.0.0.1:3128 --claude /usr/local/bin/claude

Requests
  {"cmd":"turn","prompt":"<turn>","system":"<appended system prompt>",
   "meta":{"who":"alice","role":"owner","channel":"api"},            what the harness knows: written root-owned to
                                                                      /run/sosa/turn.json before the run, so tools can
                                                                      gate on it (the agent can read it, not write it)
   "model":"claude-opus-5-5","max_turns":50,"tools":null|[...],       null: every built-in tool, permissions skipped
   "effort":"high","max_budget_usd":10,"memory":true,                 memory: Claude Code's CLAUDE.md + memory folder
   "system_mode":"append"|"replace","cwd":"/srv/app"}
     -> {"type":"delta","text":".."}  visible assistant text as it streams
        {"type":"tool","name":"Bash","input_summary":"mvn -o package"}   when a tool call starts
        {"type":"done","text":"<all visible text>","session":"..","cost_usd":0.0,"duration_ms":0,
         "fresh":false,"num_turns":1,"stop":"success"}  (+ "note" when a resume failed)
        {"type":"error","message":".."}  instead of done
     Turns run one at a time (FIFO). One that runs past --timeout is killed: error (with the partial text).
  {"cmd":"health"} -> {"ok":true,"claude":"2.1.312","token":false,"session":null,"busy":false,...}
  {"cmd":"cancel"} -> {"ok":true,"killed":bool}    kill the turn under way (its text so far is in the session)
  {"cmd":"new"}    -> {"ok":true}                   forget the session: the next turn starts a fresh conversation
  guest only (the maintenance path; there is no ssh):
  {"cmd":"token","token":".."}  -> {"ok":true}  stored root-only; null removes it. Either a `claude setup-token`
                                   token, or "relay http://127.0.0.1:PORT": Claude through the host's relay,
                                   which holds the key (sosa/relay.py)
  {"cmd":"exec","argv":[..]}    -> {"rc":0,"stdout":"..","stderr":".."}  as root ("timeout","stdin_b64","user","cwd")
  {"cmd":"put","path":"/abs","b64":"..","mode":"0644","owner":"sosa"} -> {"ok":true,"size":N}  ("append")

Client (what the host uses)
  mind.py call TARGET '<json>' | -      raw NDJSON to stdout
  mind.py say TARGET [--system-file F] [--model M] [--max-turns N] TEXT
  mind.py exec TARGET -- ARGV...        stdout/stderr through, exits with the guest's rc
  mind.py send-file TARGET LOCAL REMOTE [--mode 0644] [--owner root]   chunked put, sha256-checked
  mind.py token TARGET TOKEN_FILE       sends the token in the file (never on argv or stdout)
TARGET: hvsock:PATH[:PORT]  cloud-hypervisor's vsock unix socket: send "CONNECT PORT\\n", read "OK ..\\n",
                            then it's a byte stream to the guest's vsock PORT (default 5000)
        unix:PATH           a host mind's socket (no preamble)
        vsock:CID:PORT      plain AF_VSOCK
"""
import argparse
import base64
import collections
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_SYSTEM = "You are sosa, the agent who lives in this machine."
TURN_FILE = "/run/sosa/turn.json"
ENV_FILE = "/etc/sosa/env"
STACK_TOOLS = "/opt/sosa/stack/tools"
# Tools that run code. On the host, they would run as the host's user, so host mode drops them (--allow-bash
# keeps them). In the VM, the VM is the wall.
CODE_TOOLS = {"Bash", "BashOutput", "KillShell", "KillBash", "PowerShell", "Monitor", "WebFetch", "WebSearch",
              "Task", "Agent", "Workflow"}
VMADDR_CID_HOST = 2
MAX_LINE = 32 << 20                     # one request line (a put chunk is ~5.6 MB of base64)
PUT_CHUNK = 4 << 20
# Every claude run, guest or host: no updates, no telemetry, nobody's connectors.
CLAUDE_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}
NO_MEMORY_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------- server


class FifoLock:
    """One claude at a time, first come first served (threading.Lock makes no order promise)."""

    def __init__(self):
        self._mu = threading.Lock()
        self._waiters = collections.deque()
        self.busy = False

    def queued(self):
        return len(self._waiters)

    def acquire(self):
        with self._mu:
            if not self.busy:
                self.busy = True
                return
            ev = threading.Event()
            self._waiters.append(ev)
        ev.wait()                        # release() hands the lock straight to us

    def release(self):
        with self._mu:
            if self._waiters:
                self._waiters.popleft().set()
            else:
                self.busy = False


class Out:
    """NDJSON to one client. A client that goes away doesn't stop the turn: the agent finishes its thought."""

    def __init__(self, conn):
        self.conn = conn
        self.mu = threading.Lock()
        self.alive = True

    def send(self, obj):
        if not self.alive:
            return
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        with self.mu:
            try:
                self.conn.sendall(data)
            except OSError:
                self.alive = False


def read_env_file(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


class Mind:
    def __init__(self, a):
        self.guest = a.guest
        self.home = os.path.abspath(os.path.expanduser(a.home))
        self.cwd = os.path.abspath(os.path.expanduser(a.cwd or a.home))
        self.state = os.path.abspath(os.path.expanduser(a.state))
        self.user = a.user
        self.token_file = a.token_file
        self.token_via = a.token_via
        self.proxy = a.proxy
        self.timeout = a.timeout
        self.allow_bash = a.allow_bash
        self.claude = a.claude or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        self.turns = FifoLock()
        self.current = None              # the claude Popen under way, for cancel
        self._version = (None, None)
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        os.makedirs(self.home, exist_ok=True)
        self.pw = pwd.getpwnam(self.user) if self.user else None
        with open(os.path.abspath(__file__), "rb") as f:
            self.digest = hashlib.sha256(f.read()).hexdigest()[:12]

    # -- state

    def session(self):
        try:
            s = open(os.path.join(self.state, "session")).read().strip()
            return s if re.fullmatch(r"[0-9a-f-]{36}", s) else None
        except FileNotFoundError:
            return None

    def save_session(self, sid):
        p = os.path.join(self.state, "session")
        with open(p + ".tmp", "w") as f:
            f.write(sid + "\n")
        os.replace(p + ".tmp", p)

    def forget_session(self):
        try:
            os.unlink(os.path.join(self.state, "session"))
        except FileNotFoundError:
            pass

    def token(self):
        if not self.token_file:
            return None
        try:
            t = open(self.token_file).read().strip()
            return t or None
        except (FileNotFoundError, PermissionError):
            return None

    # -- claude

    def env(self, memory=True):
        if self.guest:
            pw = self.pw
            e = {"HOME": pw.pw_dir if pw else self.home, "USER": self.user or "root", "LOGNAME": self.user or "root",
                 "SHELL": "/bin/bash", "PATH": f"{STACK_TOOLS}:/opt/sosa/bin:/usr/local/bin:/usr/bin:/bin",
                 "LANG": "C.UTF-8", "TERM": "dumb"}
            e.update(read_env_file(ENV_FILE))        # the stack's and the box's environment (bake/create)
            if self.proxy:
                for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                    e[k] = self.proxy
                e["NO_PROXY"] = e["no_proxy"] = "localhost,127.0.0.1"
        else:
            # the host's login lives under the real HOME, so keep the environment; drop a parent Claude Code
            # session's variables (a host mind may be started from inside one)
            e = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
        e.update(CLAUDE_ENV)
        if not memory:
            e.update(NO_MEMORY_ENV)
        return e

    def popen_kw(self):
        kw = {}
        if self.pw:
            kw.update(user=self.pw.pw_uid, group=self.pw.pw_gid, extra_groups=[], umask=0o022)
        return kw

    def version(self):
        try:
            st = os.stat(self.claude)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        if self._version[0] != key:
            try:
                r = subprocess.run([self.claude, "--version"], capture_output=True, text=True, timeout=30,
                                   cwd=self.home, env=self.env(), **self.popen_kw())
                v = (r.stdout.split() or [None])[0]
            except (OSError, subprocess.TimeoutExpired):
                v = None
            self._version = (key, v)
        return self._version[1]

    def tools_for(self, req):
        tools = req.get("tools")
        if tools is None:
            if self.guest:
                return None, []          # everything, permissions skipped: the VM is the wall
            tools = ["Read", "Write", "Edit", "Glob", "Grep"]
        if not isinstance(tools, list) or not all(isinstance(t, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", t)
                                                  for t in tools):
            raise ValueError("tools must be null or a list of built-in tool names")
        dropped = []
        if not self.guest and not self.allow_bash:
            dropped = [t for t in tools if t in CODE_TOOLS]
            tools = [t for t in tools if t not in CODE_TOOLS]
        return tools, dropped

    def argv(self, req, tools, sid, resume):
        model = req.get("model") or DEFAULT_MODEL
        if not re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,80}", model):
            raise ValueError("bad model name")
        max_turns = max(1, min(int(req.get("max_turns", 50)), 500))
        system = req.get("system") or DEFAULT_SYSTEM
        if not isinstance(system, str):
            raise ValueError("system must be a string")
        mode = req.get("system_mode") or "append"
        if mode not in ("append", "replace"):
            raise ValueError("system_mode must be append or replace")
        a = [self.claude, "-p",
             "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--system-prompt" if mode == "replace" else "--append-system-prompt", system,
             # "on" (the default) would record the first turn's system prompt and reuse it forever; the host
             # sends the brief every turn and that is the one that should count
             "--system-prompt-snapshot", "off",
             "--model", model, "--max-turns", str(max_turns),
             "--strict-mcp-config", "--disable-slash-commands"]
        if tools is None:
            a += ["--dangerously-skip-permissions"]
        else:
            a += ["--tools", ",".join(tools), "--permission-mode", "dontAsk"]
            if tools:
                a += ["--allowedTools", ",".join(tools)]
        if not self.guest:
            a += ["--restricted"]        # file tools confined to the home dir, user settings/hooks ignored
        if req.get("effort"):
            if req["effort"] not in ("low", "medium", "high", "xhigh", "max"):
                raise ValueError("bad effort")
            a += ["--effort", req["effort"]]
        if req.get("max_budget_usd") is not None:
            a += ["--max-budget-usd", str(float(req["max_budget_usd"]))]
        a += ["--resume", sid] if resume else ["--session-id", sid]
        return a

    def summarize(self, name, inp):
        if not isinstance(inp, dict):
            return ""
        s = ""
        for k in ("file_path", "notebook_path", "path"):
            v = inp.get(k)
            if isinstance(v, str) and v:
                s = os.path.relpath(v, self.cwd) if v.startswith(self.cwd + "/") else v
                break
        if name in ("Glob", "Grep") and isinstance(inp.get("pattern"), str):
            s = inp["pattern"] + (f" in {s}" if s else "")
        elif name == "Bash" and isinstance(inp.get("command"), str):
            s = inp["command"].strip().splitlines()[0] if inp["command"].strip() else ""
        elif not s:
            s = next((v for v in inp.values() if isinstance(v, str)), "")
        s = " ".join(s.split())
        return s if len(s) <= 120 else s[:119] + "…"

    def write_turn_file(self, meta):
        """What the harness knows about this turn, for the stack's tools (root-owned, world-readable)."""
        if not self.guest:
            return
        try:
            os.makedirs(os.path.dirname(TURN_FILE), mode=0o755, exist_ok=True)
            tmp = TURN_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({**meta, "at": time.strftime("%FT%T")}, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, TURN_FILE)
        except OSError as e:
            log(f"turn file: {e}")

    def attempt(self, req, tools, sid, resume, out):
        """One claude run. Streams deltas/tool events through out; returns what happened."""
        r = {"text": "", "session": sid, "result": None, "rc": None, "timed_out": False, "events": 0,
             "stderr": collections.deque(maxlen=40), "synthetic": None, "tools": 0, "cancelled": False}
        env = self.env(memory=req.get("memory", True))
        cwd = req.get("cwd") or self.cwd
        if not (isinstance(cwd, str) and cwd.startswith("/") and os.path.isdir(cwd)):
            raise ValueError(f"cwd {cwd!r} is not a directory")
        pass_fds = ()
        rfd = None
        tok = self.token() if self.guest else None
        relay = re.fullmatch(r"relay (http://127\.0\.0\.1:\d{1,5})", tok or "")
        if relay:
            # Claude through the host's relay, which holds the key; the placeholder only satisfies Claude Code,
            # and the relay replaces it (127.0.0.1 skips the proxy)
            env["ANTHROPIC_BASE_URL"] = relay[1]
            env["ANTHROPIC_API_KEY"] = "held-by-the-relay"
        elif tok:
            if self.token_via == "fd":
                # on a pipe, not in the environment: the agent's own Bash children inherit claude's environment
                rfd, wfd = os.pipe()
                os.write(wfd, (tok + "\n").encode())
                os.close(wfd)
                env["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] = str(rfd)
                pass_fds = (rfd,)
            else:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        env["SOSA_WHO"] = str(meta.get("who") or "")
        env["SOSA_ROLE"] = str(meta.get("role") or "")
        try:
            proc = subprocess.Popen(self.argv(req, tools, sid, resume), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
                                    start_new_session=True, pass_fds=pass_fds, **self.popen_kw())
        finally:
            if rfd is not None:
                os.close(rfd)
        self.current = proc

        def drain_err():
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    r["stderr"].append(line)
        te = threading.Thread(target=drain_err, daemon=True)
        te.start()

        def kill(reason="timed_out"):
            r[reason] = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    return
                for _ in range(50):
                    if proc.poll() is not None:
                        return
                    time.sleep(0.1)
        r["_kill"] = kill
        timer = threading.Timer(self.timeout, kill)
        timer.daemon = True
        timer.start()
        try:
            proc.stdin.write(req["prompt"].encode())
            proc.stdin.close()
        except OSError:
            pass

        blocks = {}                      # stream index -> {"kind", ...}
        streamed_msgs = set()            # message ids whose text came through as partials
        tools_seen = set()
        cur_msg = None

        def text(t):
            # a new text block (after a tool call, or in the next message) starts a new paragraph, in the deltas
            # too, so the deltas always add up to done.text
            if not t:
                return
            if r.get("_newblock") and r["text"] and not r["text"].endswith("\n\n"):
                t = ("\n" if r["text"].endswith("\n") else "\n\n") + t
            r["_newblock"] = False
            r["text"] += t
            out.send({"type": "delta", "text": t})

        def tool(tid, name, inp):
            if tid in tools_seen:
                return
            tools_seen.add(tid)
            r["tools"] += 1
            out.send({"type": "tool", "name": name, "input_summary": self.summarize(name, inp)})

        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            r["events"] += 1
            t = ev.get("type")
            if ev.get("parent_tool_use_id"):
                continue                 # a subagent's own messages
            if ev.get("session_id"):
                r["session"] = ev["session_id"]
            if t == "system" and ev.get("subtype") == "init":
                log(f"claude: model {ev.get('model')}, tools {','.join(ev.get('tools') or []) or 'none'}, "
                    f"mcp {[m.get('name') for m in ev.get('mcp_servers') or []] or 'none'}, "
                    f"permissions {ev.get('permissionMode')}")
            elif t == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "message_start":
                    cur_msg = (e.get("message") or {}).get("id")
                    blocks = {}
                elif et == "content_block_start":
                    cb = e.get("content_block") or {}
                    if cb.get("type") == "text":
                        blocks[e.get("index")] = {"kind": "text"}
                        r["_newblock"] = True
                        if cur_msg:
                            streamed_msgs.add(cur_msg)
                    elif cb.get("type") in ("tool_use", "server_tool_use"):
                        blocks[e.get("index")] = {"kind": "tool", "id": cb.get("id"), "name": cb.get("name"),
                                                  "json": ""}
                elif et == "content_block_delta":
                    d = e.get("delta") or {}
                    b = blocks.get(e.get("index"))
                    if d.get("type") == "text_delta":
                        text(d.get("text", ""))
                    elif d.get("type") == "input_json_delta" and b and b["kind"] == "tool":
                        b["json"] += d.get("partial_json", "")
                elif et == "content_block_stop":
                    b = blocks.get(e.get("index"))
                    if b and b["kind"] == "tool":
                        try:
                            inp = json.loads(b["json"]) if b["json"] else {}
                        except ValueError:
                            inp = {}
                        tool(b["id"], b["name"], inp)
            elif t == "assistant":
                m = ev.get("message") or {}
                if m.get("model") == "<synthetic>":
                    # Claude Code's own notice (an API error, a limit), not the agent talking
                    r["synthetic"] = " ".join(c.get("text", "") for c in m.get("content") or []
                                              if c.get("type") == "text").strip()
                    continue
                for c in m.get("content") or []:
                    if c.get("type") == "text" and m.get("id") not in streamed_msgs:
                        r["_newblock"] = True
                        text(c.get("text", ""))
                    elif c.get("type") in ("tool_use", "server_tool_use"):
                        tool(c.get("id"), c.get("name"), c.get("input"))
                if m.get("id"):
                    streamed_msgs.add(m["id"])
            elif t == "result":
                r["result"] = ev
        r["rc"] = proc.wait()
        self.current = None
        timer.cancel()
        te.join(timeout=2)
        return r

    def turn(self, req, out):
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return out.send({"type": "error", "message": "turn needs a prompt"})
        if self.guest and not self.token():
            return out.send({"type": "error", "message": "no token yet: `sosa token` or a relay on the host"})
        try:
            tools, dropped = self.tools_for(req)
            self.argv(req, tools, "00000000-0000-0000-0000-000000000000", False)   # validate before queueing
        except (ValueError, TypeError) as e:
            return out.send({"type": "error", "message": str(e)})
        self.turns.acquire()
        t0 = time.time()
        try:
            self.write_turn_file(req.get("meta") if isinstance(req.get("meta"), dict) else {})
            sid = self.session()
            fresh = sid is None
            note = None
            try:
                if sid:
                    r = self.attempt(req, tools, sid, True, out)
                    errs = " ".join((r["result"] or {}).get("errors") or []) or " ".join(r["stderr"])
                    if not r["text"] and not r["timed_out"] and not r["cancelled"] and "No conversation found" in errs:
                        note = f"could not resume session {sid} ({errs.strip()[:200]}); started a fresh one"
                        log("turn: " + note)
                        sid = None
                if sid is None:
                    sid, fresh = str(uuid.uuid4()), True
                    r = self.attempt(req, tools, sid, False, out)
            except (ValueError, OSError) as e:
                return out.send({"type": "error", "message": str(e)})
            res = r["result"] or {}
            # keep the session only if the agent did something in it: a turn that failed before it said or did
            # anything (no token, a 401) shouldn't become the start of its conversation
            ran = r["text"] or r["tools"] or r["timed_out"] or r["cancelled"]
            if ran and r["session"]:
                self.save_session(r["session"])
            dur = res.get("duration_ms") or int((time.time() - t0) * 1000)
            if r["timed_out"] or r["cancelled"]:
                why = f"turn killed after {self.timeout:g} s" if r["timed_out"] else "turn cancelled"
                ev = {"type": "error", "message": why, "text": r["text"], "session": r["session"],
                      "cost_usd": res.get("total_cost_usd") or 0.0}
            elif not r["text"] and (r["rc"] != 0 or res.get("is_error") or not res):
                why = (r["synthetic"] or " ".join(res.get("errors") or []) or
                       (res.get("result") if isinstance(res.get("result"), str) else "") or
                       (r["stderr"][-1] if r["stderr"] else f"claude exited {r['rc']}"))
                ev = {"type": "error", "message": why.strip()[:2000], "cost_usd": res.get("total_cost_usd") or 0.0}
            else:
                ev = {"type": "done", "text": r["text"], "session": r["session"],
                      "cost_usd": res.get("total_cost_usd") or 0.0, "duration_ms": dur, "fresh": fresh,
                      "num_turns": res.get("num_turns"), "stop": res.get("subtype"), "tools": r["tools"]}
            if note:
                ev["note"] = note
            if dropped:
                ev["dropped_tools"] = dropped
            log(f"turn: {ev['type']} {len(prompt)} chars in, {len(r['text'])} out, {r['tools']} tools, {dur} ms, "
                f"${res.get('total_cost_usd') or 0:.4f}, session {r['session']}{' (fresh)' if fresh else ''}"
                + ("" if ev["type"] == "done" else f": {ev['message'][:200]}"))
            out.send(ev)
        finally:
            self.turns.release()

    # -- the rest

    def health(self, req, out):
        tok = self.token()
        out.send({"ok": True, "claude": self.version(), "token": bool(tok), "session": self.session(),
                  "home": self.home, "cwd": self.cwd, "mode": "guest" if self.guest else "host",
                  "auth": (("relay" if (tok or "").startswith("relay ") else "token") if tok else None)
                          if self.guest else "login",
                  "busy": self.turns.busy, "queued": self.turns.queued(), "mind": self.digest})

    def cancel(self, req, out):
        p = self.current
        if p is None or p.poll() is not None:
            return out.send({"ok": True, "killed": False})
        log("cancel: killing the turn under way")
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # mark it so turn() reports "cancelled" rather than a plain failure
        for _ in range(100):
            if p.poll() is not None:
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out.send({"ok": True, "killed": True})

    def new(self, req, out):
        self.forget_session()
        log("session forgotten: the next turn starts fresh")
        out.send({"ok": True})

    def set_token(self, req, out):
        tok = req.get("token")
        if tok is None:
            try:
                os.unlink(self.token_file)
            except FileNotFoundError:
                pass
            log("token removed")
            return out.send({"ok": True})
        tok = tok.strip() if isinstance(tok, str) else ""
        if not (re.fullmatch(r"[A-Za-z0-9._~+/=-]{20,1000}", tok)
                or re.fullmatch(r"relay http://127\.0\.0\.1:\d{1,5}", tok)):
            return out.send({"ok": False, "error": "that doesn't look like a token or a relay address"})
        os.makedirs(os.path.dirname(self.token_file), mode=0o700, exist_ok=True)
        tmp = self.token_file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n")
        os.chown(tmp, 0, 0)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_file)
        log("token stored" if not tok.startswith("relay ") else f"auth: {tok}")
        out.send({"ok": True})

    def exec(self, req, out):
        argv = req.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            return out.send({"rc": 2, "stdout": "", "stderr": "exec needs argv: a list of strings"})
        timeout = max(1, min(int(req.get("timeout", 120)), 7200))
        stdin = base64.b64decode(req["stdin_b64"]) if req.get("stdin_b64") else b""
        env = {"PATH": f"{STACK_TOOLS}:/opt/sosa/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "LANG": "C.UTF-8", "HOME": "/root", "TERM": "dumb"}
        env.update(read_env_file(ENV_FILE))
        if isinstance(req.get("env"), dict):
            env.update({str(k): str(v) for k, v in req["env"].items() if re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(k))})
        kw = {}
        if req.get("user"):
            try:
                pw = pwd.getpwnam(req["user"])
            except KeyError:
                return out.send({"rc": 2, "stdout": "", "stderr": f"no such user {req['user']}"})
            kw.update(user=pw.pw_uid, group=pw.pw_gid, extra_groups=[])
            env.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
        cwd = req.get("cwd") or "/"
        log(f"exec: {' '.join(argv)[:200]}")
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, cwd=cwd, env=env, **kw)
            rc, so, se = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            rc, so, se = 124, e.stdout or b"", (e.stderr or b"") + f"\n(timed out after {timeout} s)".encode()
        except OSError as e:
            rc, so, se = 127, b"", str(e).encode()
        out.send({"rc": rc, "stdout": so.decode("utf-8", "replace"), "stderr": se.decode("utf-8", "replace")})

    def put(self, req, out):
        path = req.get("path")
        if not isinstance(path, str) or not path.startswith("/") or os.path.normpath(path) != path:
            return out.send({"ok": False, "error": "path must be absolute and normalized"})
        data = base64.b64decode(req.get("b64") or "")
        mode = int(str(req.get("mode", "0644")), 8)
        try:
            pw = pwd.getpwnam(req.get("owner") or "root")
        except KeyError:
            return out.send({"ok": False, "error": "no such owner"})
        parent = os.path.dirname(path)
        missing = []
        while not os.path.isdir(parent):
            missing.append(parent)
            parent = os.path.dirname(parent)
        for d in reversed(missing):
            os.mkdir(d, 0o755)
            os.chown(d, pw.pw_uid, pw.pw_gid)
        if req.get("append"):
            with open(path, "ab") as f:
                f.write(data)
            os.chown(path, pw.pw_uid, pw.pw_gid)
            os.chmod(path, mode)
        else:
            tmp = path + ".put-tmp"
            with open(tmp, "wb") as f:
                f.write(data)
                os.fchown(f.fileno(), pw.pw_uid, pw.pw_gid)
                os.fchmod(f.fileno(), mode)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        out.send({"ok": True, "size": os.path.getsize(path)})

    # -- connections

    def handle(self, conn, peer):
        out = Out(conn)
        try:
            if self.guest and isinstance(peer, tuple) and peer and peer[0] != VMADDR_CID_HOST:
                log(f"refused a connection from vsock cid {peer[0]} (only the host may talk to the mind)")
                return
            line = conn.makefile("rb").readline(MAX_LINE)
            try:
                req = json.loads(line)
                cmd = req["cmd"]
            except (ValueError, KeyError, TypeError):
                return out.send({"type": "error", "message": "send one JSON object with a cmd, then a newline"})
            handlers = {"turn": self.turn, "health": self.health, "cancel": self.cancel, "new": self.new}
            if self.guest:
                handlers.update(token=self.set_token, exec=self.exec, put=self.put)
            h = handlers.get(cmd)
            if not h:
                return out.send({"type": "error", "message": f"unknown cmd {cmd!r}" +
                                 ("" if self.guest else " (token/exec/put are VM-only)")})
            h(req, out)
        except Exception as e:           # noqa: BLE001 - one bad request must not take the mind down
            log(f"error: {type(e).__name__}: {e}")
            out.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve(self, spec):
        kind, _, rest = spec.partition(":")
        if kind == "vsock":
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            s.bind((socket.VMADDR_CID_ANY, int(rest)))
        elif kind == "unix":
            path = os.path.expanduser(rest)
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)        # the socket is 0600: only this user may talk to the mind
            try:
                s.bind(path)
            finally:
                os.umask(old)
        else:
            raise SystemExit(f"--listen takes vsock:PORT or unix:PATH, not {spec}")
        s.listen(16)
        log(f"mind {self.digest} listening on {spec} ({'guest' if self.guest else 'host'}; home {self.home}, "
            f"cwd {self.cwd}, claude {self.version()}, session {self.session()})")
        while True:
            conn, peer = s.accept()
            threading.Thread(target=self.handle, args=(conn, peer), daemon=True).start()


# ---------------------------------------------------------------------------------------------- client


def connect(target, timeout=600):
    kind, _, rest = target.partition(":")
    if kind == "hvsock":
        path, port = rest, 5000
        m = re.fullmatch(r"(.*):(\d+)", rest)
        if m:
            path, port = m.group(1), int(m.group(2))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(os.path.expanduser(path))
        s.sendall(f"CONNECT {port}\n".encode())
        line = b""
        while not line.endswith(b"\n"):
            c = s.recv(1)
            if not c:
                raise ConnectionError(f"nothing is listening on the guest's vsock port {port} "
                                      "(still booting, or sosa-mind is down)")
            line += c
        if not line.startswith(b"OK"):
            raise ConnectionError(f"hybrid vsock said {line!r}")
    elif kind == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(os.path.expanduser(rest))
    elif kind == "vsock":
        cid, port = rest.split(":")
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((int(cid), int(port)))
    else:
        raise SystemExit(f"target must be hvsock:PATH[:PORT], unix:PATH or vsock:CID:PORT, not {target}")
    s.settimeout(timeout)
    return s


def request(target, obj, timeout=600):
    """Yields each response object."""
    s = connect(target, timeout)
    try:
        s.sendall((json.dumps(obj) + "\n").encode())
        for line in s.makefile("rb"):
            if line.strip():
                yield json.loads(line)
    finally:
        s.close()


def one(target, obj, timeout=600):
    for ev in request(target, obj, timeout):
        return ev
    raise ConnectionError("the mind closed the connection without answering")


def client_exec(target, argv, timeout=600, user=None, cwd=None):
    req = {"cmd": "exec", "argv": argv, "timeout": timeout}
    if user:
        req["user"] = user
    if cwd:
        req["cwd"] = cwd
    ev = one(target, req, timeout + 30)
    if "rc" not in ev:
        raise SystemExit(f"mind.py: {ev}")
    sys.stdout.write(ev.get("stdout", ""))
    sys.stderr.write(ev.get("stderr", ""))
    return ev["rc"]


def send_file(target, local, remote, mode="0644", owner="root"):
    h = hashlib.sha256()
    tmp = remote + ".part"
    size = 0
    with open(local, "rb") as f:
        first = True
        while True:
            chunk = f.read(PUT_CHUNK)
            if not chunk and not first:
                break
            h.update(chunk)
            size += len(chunk)
            ev = one(target, {"cmd": "put", "path": tmp, "b64": base64.b64encode(chunk).decode(), "mode": mode,
                              "owner": owner, "append": not first})
            if not ev.get("ok"):
                raise SystemExit(f"mind.py: put {tmp}: {ev}")
            first = False
            if not chunk:
                break
    ev = one(target, {"cmd": "exec", "argv": ["sha256sum", tmp]})
    if (ev.get("stdout") or "").split()[:1] != [h.hexdigest()]:
        raise SystemExit(f"mind.py: {remote}: checksum mismatch after upload ({ev})")
    ev = one(target, {"cmd": "exec", "argv": ["mv", "-f", tmp, remote]})
    if ev.get("rc") != 0:
        raise SystemExit(f"mind.py: mv into {remote}: {ev.get('stderr')}")
    return size


def say(target, a):
    system = open(a.system_file).read() if a.system_file else DEFAULT_SYSTEM
    req = {"cmd": "turn", "system": system, "prompt": a.text, "model": a.model, "max_turns": a.max_turns,
           "tools": a.tools.split(",") if a.tools else None}
    tty = sys.stderr.isatty()
    dim = (lambda s: f"\x1b[2m{s}\x1b[0m") if tty else (lambda s: s)
    for ev in request(target, req, timeout=a.timeout):
        t = ev.get("type")
        if t == "delta":
            sys.stdout.write(ev["text"])
            sys.stdout.flush()
        elif t == "tool":
            print(dim(f"\n[{ev['name']} {ev.get('input_summary', '')}]"), file=sys.stderr, flush=True)
        elif t == "done":
            print(flush=True)
            extra = f", {ev['note']}" if ev.get("note") else ""
            extra += f", dropped {','.join(ev['dropped_tools'])}" if ev.get("dropped_tools") else ""
            print(dim(f"(session {ev.get('session')}{' fresh' if ev.get('fresh') else ''}, "
                      f"${ev.get('cost_usd') or 0:.4f}, {(ev.get('duration_ms') or 0) / 1000:.1f} s, "
                      f"{ev.get('num_turns')} turns, {ev.get('stop')}{extra})"), file=sys.stderr)
            return 0
        elif t == "error":
            print(file=sys.stderr)
            print(f"error: {ev.get('message')}", file=sys.stderr)
            return 1
    print("\nerror: the mind closed the connection mid-turn", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description="the agent's mind (see the top of this file)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--listen", required=True, help="vsock:PORT or unix:PATH")
    s.add_argument("--home", required=True, help="the agent user's home (HOME)")
    s.add_argument("--cwd", help="where claude runs (default: the home)")
    s.add_argument("--state", required=True, help="where the session id lives")
    s.add_argument("--guest", action="store_true", help="in the VM: token/exec/put, host-only peers, proxy")
    s.add_argument("--user", help="run claude as this user (guest)")
    s.add_argument("--token-file", help="the setup-token token or the relay line (guest)")
    s.add_argument("--token-via", choices=["fd", "env"], default="fd",
                   help="hand the token to claude on a pipe (default) or as CLAUDE_CODE_OAUTH_TOKEN")
    s.add_argument("--proxy", help="HTTPS_PROXY for claude and its children (guest)")
    s.add_argument("--claude", help="claude binary (default: the one on PATH)")
    s.add_argument("--timeout", type=float, default=1800, help="seconds before a turn is killed")
    s.add_argument("--allow-bash", action="store_true", help="host mode: keep Bash and other code tools")
    c = sub.add_parser("call")
    c.add_argument("target")
    c.add_argument("json", help="the request, or - for stdin")
    y = sub.add_parser("say")
    y.add_argument("target")
    y.add_argument("text")
    y.add_argument("--system-file")
    y.add_argument("--model", default=DEFAULT_MODEL)
    y.add_argument("--max-turns", type=int, default=50)
    y.add_argument("--tools", help="comma-separated (default: everything)")
    y.add_argument("--timeout", type=float, default=1800)
    e = sub.add_parser("exec")
    e.add_argument("target")
    e.add_argument("--timeout", type=int, default=600)
    e.add_argument("--user")
    e.add_argument("--cwd")
    e.add_argument("argv", nargs=argparse.REMAINDER)
    f = sub.add_parser("send-file")
    f.add_argument("target")
    f.add_argument("local")
    f.add_argument("remote")
    f.add_argument("--mode", default="0644")
    f.add_argument("--owner", default="root")
    k = sub.add_parser("token")
    k.add_argument("target")
    k.add_argument("token_file")
    a = ap.parse_args()

    try:
        if a.cmd == "serve":
            if a.guest and not a.token_file:
                ap.error("--guest needs --token-file")
            Mind(a).serve(a.listen)
        elif a.cmd == "call":
            obj = json.loads(sys.stdin.read() if a.json == "-" else a.json)
            for ev in request(a.target, obj):
                print(json.dumps(ev, ensure_ascii=False), flush=True)
        elif a.cmd == "say":
            sys.exit(say(a.target, a))
        elif a.cmd == "exec":
            argv = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
            if not argv:
                ap.error("exec needs a command")
            sys.exit(client_exec(a.target, argv, a.timeout, a.user, a.cwd))
        elif a.cmd == "send-file":
            n = send_file(a.target, a.local, a.remote, a.mode, a.owner)
            print(f"{a.remote}: {n} bytes")
        elif a.cmd == "token":
            tok = open(a.token_file).read().strip()
            ev = one(a.target, {"cmd": "token", "token": tok})
            del tok
            if not ev.get("ok"):
                raise SystemExit(f"mind.py: the VM refused the token: {ev.get('error')}")
            print("token stored in the VM")
    except (OSError, ValueError) as err:     # no socket, refused, timed out, path too long, bad JSON
        raise SystemExit(f"mind.py: {getattr(a, 'target', a.cmd)}: {err}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
