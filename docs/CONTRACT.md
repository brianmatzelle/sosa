# The stack contract

A **stack** is a directory. sosa never knows what's in it; it only runs files at fixed names, with a fixed
environment, and reads prose from fixed files. If a stack passes `sosa stack check`, sosa will run it. That's
the whole contract, enforced rather than described. `sosa stack init DIR` writes a skeleton.

```
stacks/<name>/
  stack.toml        name, [box] defaults, [agent], [app], [egress], [env], [gate]
  brief.md          REQUIRED. The stack's part of the system prompt: where the code is, what the tools do,
                    the template's conventions, what's newer than the model remembers
  bake.sh           runs once, as root, in a fresh box with the network open, to make the stack's image:
                    toolchains (pinned, checksummed), the seed built once so every cache is warm
  seed/             the app a new box starts with; copied to app.dir on first boot if nothing is there
  tools/            executables put on the agent's PATH (/opt/sosa/stack/tools); help at the top of each
  facts.sh          printed in front of every turn: live facts (runs as the agent user, in agent.cwd, 20 s)
  home/             files for the agent's home on first boot (a CLAUDE.md, say); never overwrites
  changes.paths     what the changes history tracks, one path per line from / (sosa adds its own)
  changes.exclude   extra git exclude patterns for it
```

## stack.toml

```toml
name = "java-react"                  # [a-z][a-z0-9-]*, up to 32
description = "a templated Java backend and React frontend"

[box]                                # defaults for boxes made from it
mem = "4G"
cpus = 2
disk = "20G"

[agent]
user = "sosa"                        # the unix user the agent runs as
sudo = true                          # root inside the box; the box is the wall
cwd = "/srv/app"                     # where claude runs; default app.dir
model = "claude-sonnet-5-5"
tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]   # absent: every built-in tool, permissions skipped
max_turns = 50
timeout_s = 1800                     # a turn is killed past this; its text so far is kept
memory = true                        # Claude Code's CLAUDE.md and memory folder inside the box
effort = "high"                      # optional
max_budget_usd = 10                  # a turn's cap (Claude Code's own); the relay caps the day

[app]                                # what the keeper runs; leave the table out for a box with no app
dir = "/srv/app"
build = "tools/build"                # a shell line, in dir. `tools/x` means the stack's tools dir
run = "tools/run"                    # stays in the foreground; listens on $PORT (a spare port, not the stable one)
health = "tools/health"              # exit 0 when the instance on $PORT is up
ports = [8080, 5173]                 # the first is the stable one the keeper swaps behind; the rest pass through
ready_s = 90                         # how long health may take after start
[app.env]                            # extra environment for build, run and health
SPRING_PROFILES_ACTIVE = "box"

[egress]                             # hosts the box may CONNECT to, besides api.anthropic.com:443
allow = ["repo.maven.apache.org:443", "registry.npmjs.org:443", "*.github.com:443"]

[env]                                # /etc/sosa/env: in the agent's, the tools' and the keeper's environment
MAVEN_OPTS = "-Dmaven.repo.local=/srv/m2"

[gate]                               # tools that are the owner's call; enforced by the tools (below)
privileged = ["deploy", "db-reset"]
```

## What the box gives the stack

- **The environment** of every command the agent runs, every tool, `facts.sh` and the keeper: `PATH` with
  `/opt/sosa/stack/tools` first, then `/opt/sosa/bin` (`app`, `changes`), the `[env]` table, `SOSA_STACK`,
  `SOSA_USER`, `SOSA_BOX`, `APP_DIR`, `STABLE_PORT`, and for the keeper's `build`/`run`/`health` also `PORT`.
  The agent's commands also get `HTTPS_PROXY=http://127.0.0.1:3128` (the egress) and `SOSA_WHO`/`SOSA_ROLE`
  (informational; the gate reads the file below).
- **The turn file** `/run/sosa/turn.json`, root-owned, world-readable, written before each turn:
  `{"who": "alice", "role": "owner"|"guest"|"admin", "channel": "api", "box": "t1", "at": "..."}`. A tool that is
  the owner's call reads `role` there and refuses otherwise. The agent can read it and can't write it, so the
  gate holds even when the agent is talked into something.
- **The keeper** (`app`): `app build`, `app load`, `app status`, `app log`, `app stop`, `app rollback`.
  `load` builds, starts the new build beside the live one on `stable + 10000 + slot`, waits for `health`, points
  the stable port at it (a proxy sosa runs on that port), stops the old one, and after 15 s up records the
  changes rev as the last good one. If the new one won't come up, the live one stays.
- **The changes history** (`changes`): git over `/` with a path list; sosa commits after every turn, named for
  whose turn it was. The stack's `changes.paths` seeds the list; the app dir belongs on it.
- **The preview**: each port in `app.ports` is reachable on the host at `127.0.0.1:<box's preview port + i>`,
  over vsock (no NIC, no DNAT, no root). `sosa preview NAME` prints them; your backend maps a user to them.
- **Facts**: what `facts.sh` prints goes under the header of every turn. Make them live (what's running, the
  last build, which branch) rather than remembered.

## Baking

`sosa image STACK` makes a fresh box from the plain Ubuntu image, with the network open through the proxy
(`HTTPS_PROXY`/`HTTP_PROXY` in the environment, apt configured), runs `bake.sh` as root in `/opt/sosa/stack`,
then proves the app (`app load` must come up healthy), wipes what makes a box someone's (the agent's Claude
state, the session, the token, the changes history, logs), and keeps the disk as `images/STACK.raw`. Boxes made
after that start from it in seconds, with warm caches. Bake rules of thumb:

- Pin and checksum what you download. The bake is the only time the box has the network.
- Build the seed once in the bake, so `.m2`, the npm cache and the like are warm; afterwards the box can only
  reach what `[egress] allow` lists. Tools that don't honour the proxy variables need their own setting
  (Maven's `settings.xml`, Gradle's `gradle.properties`).
- Leave the built seed in place at `app.dir`: a new box starts from a skeleton that already works.
- Anything that is per box (keys, hostnames, data) is not for the bake; it comes with `[env]` or a turn.

## The check

`sosa stack check STACK` is the test: the files (names, executability, the tables), then a bake, a box from the
image, `app load` in it, one owner turn, and the box is destroyed. `--quick` stops after the files. Run it in CI
for your stacks; if it passes, sosa will run the stack.

## Where the line is

The rule for what belongs on which side: anything that names a toolchain, a program, a port, a mirror, a
build step, a fixed tool, or a "where you are" paragraph is a stack. The VM, the wall, the relay, the mind, the
changes history, the keeper, and the harness header are sosa. If a change to sosa would only make sense for
one stack, it belongs in that stack.
