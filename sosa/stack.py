"""stack.py: a stack is a directory with fixed filenames; sosa never knows what's in it. This reads and checks one.

    stack.toml      name, [box] defaults, [agent], [app], [egress], [env], [gate]   (see docs/CONTRACT.md)
    brief.md        the stack's part of the system prompt (required)
    bake.sh         runs once in a fresh box to make the stack's image (optional)
    seed/           the app a new box starts with, copied to app.dir on first boot (optional)
    tools/          executables on the agent's PATH (optional)
    facts.sh        prints live facts put in front of every turn (optional)
    home/           files for the agent's home on first boot (optional)
    changes.paths   paths the changes history tracks, one per line from / (optional)
    changes.exclude git exclude patterns for it (optional)
"""
import json
import os
import re
import stat
import tomllib

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DEFAULTS = {
    "box": {"mem": "2G", "cpus": 2, "disk": "10G"},
    "agent": {"user": "sosa", "sudo": True, "cwd": None, "model": "claude-sonnet-5-5", "tools": None,
              "max_turns": 50, "timeout_s": 1800, "memory": True, "effort": None, "max_budget_usd": 10.0},
    "app": {"dir": "/srv/app", "build": None, "run": None, "health": None, "ports": [], "ready_s": 60, "env": {}},
    "egress": {"allow": []},
    "env": {},
    "gate": {"privileged": []},
}


class StackError(Exception):
    pass


class Stack:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        tp = os.path.join(self.path, "stack.toml")
        try:
            with open(tp, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError:
            raise StackError(f"{self.path} has no stack.toml (a stack is a directory with one; `sosa stack init`)")
        except tomllib.TOMLDecodeError as e:
            raise StackError(f"{tp}: {e}")
        self.name = raw.get("name")
        self.description = raw.get("description") or ""
        self.t = {}
        for sect, defs in DEFAULTS.items():
            got = raw.get(sect) or {}
            if not isinstance(got, dict):
                raise StackError(f"[{sect}] must be a table")
            self.t[sect] = {**defs, **got}
        if not self.t["agent"]["cwd"]:
            self.t["agent"]["cwd"] = self.t["app"]["dir"]

    # -- what's in it
    def file(self, name):
        p = os.path.join(self.path, name)
        return p if os.path.exists(p) else None

    @property
    def box(self):
        return self.t["box"]

    @property
    def agent(self):
        return self.t["agent"]

    @property
    def app(self):
        return self.t["app"]

    @property
    def has_app(self):
        return bool(self.app.get("run") and self.app.get("ports"))

    def brief(self):
        p = self.file("brief.md")
        return open(p).read() if p else ""

    def app_json(self):
        a = self.app
        return {"dir": a["dir"], "build": a["build"], "run": a["run"], "health": a["health"],
                "ports": [int(x) for x in a["ports"]], "ready_s": a["ready_s"], "env": a["env"]}

    def env_lines(self, extra=None):
        env = {"SOSA_STACK": self.name, "SOSA_USER": self.agent["user"], "APP_DIR": self.app["dir"]}
        env.update({str(k): str(v) for k, v in self.t["env"].items()})
        if self.app.get("ports"):
            env["STABLE_PORT"] = str(self.app["ports"][0])
        env.update(extra or {})
        return "".join(f"{k}={v}\n" for k, v in env.items())

    def egress_allow(self):
        return ["api.anthropic.com:443"] + [str(x) for x in self.t["egress"]["allow"]]

    # -- is it well formed
    def problems(self):
        p = []
        if not self.name or not NAME_RE.match(self.name):
            p.append("name must be [a-z][a-z0-9-]* (up to 32 chars)")
        if not self.file("brief.md"):
            p.append("brief.md is missing: the stack's part of the system prompt, what the agent is told about where it is")
        elif not self.brief().strip():
            p.append("brief.md is empty")
        for f in ("bake.sh", "facts.sh"):
            fp = self.file(f)
            if fp and not os.stat(fp).st_mode & stat.S_IXUSR:
                p.append(f"{f} is not executable (chmod +x)")
        td = self.file("tools")
        if td:
            for n in sorted(os.listdir(td)):
                fp = os.path.join(td, n)
                if os.path.isfile(fp) and not os.stat(fp).st_mode & stat.S_IXUSR:
                    p.append(f"tools/{n} is not executable (chmod +x)")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", n):
                    p.append(f"tools/{n}: name it like a command ([A-Za-z0-9_.-])")
        a = self.app
        if a.get("run") and not a.get("ports"):
            p.append("[app] has run but no ports: the first port is the stable one the keeper swaps behind")
        if a.get("ports") and not a.get("run"):
            p.append("[app] has ports but no run")
        for x in a.get("ports") or []:
            if not isinstance(x, int) or not 1 <= x <= 65535:
                p.append(f"[app] ports: {x!r} is not a port")
        if a.get("ports") and a["ports"][0] + 10000 > 65535:
            p.append("[app] the stable port must be below 55536 (instances run at port + 10000 + slot)")
        if not str(a.get("dir", "")).startswith("/"):
            p.append("[app] dir must be absolute")
        ag = self.agent
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(ag.get("user", ""))):
            p.append("[agent] user must be a unix user name")
        if ag.get("tools") is not None and not (isinstance(ag["tools"], list) and all(isinstance(t, str) for t in ag["tools"])):
            p.append("[agent] tools must be a list of tool names, or absent for everything")
        for k in ("max_turns", "timeout_s"):
            if not isinstance(ag.get(k), (int, float)) or ag[k] <= 0:
                p.append(f"[agent] {k} must be a positive number")
        if ag.get("effort") not in (None, "", "low", "medium", "high", "xhigh", "max"):
            p.append("[agent] effort must be low, medium, high, xhigh or max")
        for h in self.t["egress"]["allow"]:
            if not re.fullmatch(r"(\*\.)?[A-Za-z0-9.-]+:\d{1,5}", str(h)):
                p.append(f"[egress] allow: {h!r} must be host:port or *.domain:port")
        for k in self.t["env"]:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
                p.append(f"[env] {k}: not a variable name")
        if not re.fullmatch(r"\d+[MG]", str(self.box.get("mem", ""))):
            p.append("[box] mem must look like 2G or 512M")
        if not re.fullmatch(r"\d+[MG]", str(self.box.get("disk", ""))):
            p.append("[box] disk must look like 10G")
        return p

    def describe(self):
        d = {"name": self.name, "description": self.description, "path": self.path,
             "files": sorted(n for n in os.listdir(self.path) if not n.startswith(".")),
             "has_app": self.has_app, "ports": self.app.get("ports"), "agent": self.agent, "box": self.box,
             "egress": self.egress_allow(), "problems": self.problems()}
        td = self.file("tools")
        d["tools"] = sorted(os.listdir(td)) if td else []
        return d


def load(path):
    return Stack(path)


INIT_TOML = '''name = "{name}"
description = "what this stack is, in a line"

[box]                 # defaults for boxes made from this stack
mem = "2G"
cpus = 2
disk = "10G"

[agent]
user = "sosa"         # the unix user the agent runs as
sudo = true           # root inside the box; the box is the wall
cwd = "/srv/app"      # where claude runs
model = "claude-sonnet-5-5"
max_turns = 50
timeout_s = 1800      # a turn is killed past this
memory = true         # Claude Code's CLAUDE.md and memory folder, inside the box

[app]                 # what the keeper (app build / app load) runs; leave out for a box with no app
dir = "/srv/app"
build = "tools/build" # a shell line, in dir, with tools/ on PATH
run = "tools/run"     # stays in the foreground, listens on $PORT
health = "tools/health"   # exit 0 when the instance on $PORT is up
ports = [8080]        # the first is the stable one; the rest pass straight through
ready_s = 60

[egress]              # hosts the box may reach, besides Claude: an artifact mirror, a git host
allow = []

[env]                 # /etc/sosa/env, in the agent's and the tools' environment

[gate]                # tools that refuse unless /run/sosa/turn.json says role is owner (enforced by the tools)
privileged = []
'''

INIT_BRIEF = '''# Where you are

You are the agent of a box made from the "{name}" stack. (Replace this with what the agent needs to know:
where the code is, what the tools in `tools/` do, the conventions of the template, what is newer than you
remember. The generic parts, who the owner is, the changes history, how turns work, come from sosa.)
'''

INIT_FACTS = '''#!/usr/bin/env bash
# facts.sh: printed in front of every turn. Live facts, not remembered ones: what's running, what the last build
# said, which branch the app is on. Runs as the agent user, in the app dir, with a 20 s timeout. Keep it short.
cd "${{APP_DIR:-/srv/app}}" 2>/dev/null || exit 0
echo "app dir: $PWD"
command -v app >/dev/null && app status 2>/dev/null | head -3
'''


def init(path, name):
    os.makedirs(path, exist_ok=True)
    if os.path.exists(os.path.join(path, "stack.toml")):
        raise StackError(f"{path} already has a stack.toml")
    for d in ("seed", "tools"):
        os.makedirs(os.path.join(path, d), exist_ok=True)
    with open(os.path.join(path, "stack.toml"), "w") as f:
        f.write(INIT_TOML.format(name=name))
    with open(os.path.join(path, "brief.md"), "w") as f:
        f.write(INIT_BRIEF.format(name=name))
    fp = os.path.join(path, "facts.sh")
    with open(fp, "w") as f:
        f.write(INIT_FACTS.format())
    os.chmod(fp, 0o755)
    with open(os.path.join(path, "changes.paths"), "w") as f:
        f.write("# paths the changes history tracks, from /, one per line (the app dir is a good start)\nsrv/app\n")
    return path


if __name__ == "__main__":
    import sys
    s = load(sys.argv[1])
    print(json.dumps(s.describe(), indent=2))
