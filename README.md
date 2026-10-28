# sosa

super.open.source.agent

An agent in a box.

A **box** is a small virtual machine with no network interface. In it lives one agent: Claude Code, with a shell,
root if you say so, a home that is its memory, and the code it's there to work on. The only ways in or out are a
socket to the host: turns come in, the model's answers come back through a relay that holds the key, and the
app's ports come out as a preview. The box is the wall, and the wall is what lets you hand the agent the whole
machine.

A **stack** is what goes in the box: your toolchain, your templates, your tools, and a page telling the agent
where it is. sosa knows nothing about Java or React or game servers. It knows how to make a box, keep it, talk to
it, and run whatever stack you give it. See [docs/CONTRACT.md](docs/CONTRACT.md).

```
your backend / the sosa CLI ──► sosad (HTTP on a unix socket, or TCP with a bearer token)
                                  ├─ relay: Claude, the key on the host, a budget a box a day
                                  ├─ egress: a CONNECT proxy, the hosts the stack allows and nothing else
                                  ├─ previews: 127.0.0.1:18000+ ──vsock──► the app's ports in the box
                                  └─ one cloud-hypervisor per box (KVM; no NIC; vsock only)
                                        └─ the box: mind.py (claude -p, one conversation, resumed every turn)
                                                    app (the keeper: build, load live, roll back)
                                                    changes (git over /, a commit after every turn)
                                                    /opt/sosa/stack: your stack, on the agent's PATH
```

## Quick start

```sh
./install.sh                          # the hypervisor, the Ubuntu image, a user unit for the daemon
export PATH=$PWD/bin:$PATH
sosa key                              # the Claude API key (or a `claude setup-token` token) the relay holds
sosa stack add stacks/hello           # the smallest stack: a stdlib Python web app
sosa stack check hello                # the contract test: bake, a box from the image, app load, one turn
sosa create t1 --stack hello --start  # a box of your own
sosa ask t1 "change the greeting on / and load it live"
sosa preview t1                       # http://127.0.0.1:18000 -> the app's stable port in the box
sosa chat t1                          # a conversation
```

Requirements: Linux with `/dev/kvm`, Python 3.11+ at `/usr/bin/python3`, `xorriso`, `mke2fs`, `debugfs`,
`sfdisk`, `curl`, and Claude Code at `~/.local/bin/claude` (it's copied into every box). The host is whatever
can run KVM: bare metal, or a cloud instance with nested virtualization. Everything is Python's standard library
and bash; there is nothing to install beyond that.

## What a box is made of

- **The wall.** No network interface. Claude reaches the box through the relay; the stack's allowed hosts (an
  artifact mirror, a git host) through the egress proxy; nothing else does. The host reaches the box over vsock:
  the mind, the preview. The key is never in the box. Root in the box gets exactly the box.
- **The mind** (`sosa/guest/mind.py`). `claude -p` with one conversation, resumed every turn, the system prompt
  rebuilt every turn (sosa's harness page, the stack's brief, the owner's prompt), the tools the stack allows,
  a timeout, a budget. Turns run one at a time, first come first served; a client that goes away doesn't stop
  one. The agent's Claude Code memory (`CLAUDE.md`, the memory folder) lives in its home, inside the box.
- **Who's talking comes from the harness, never the message.** Every turn starts with
  `[channel · from who · ROLE]`, which your backend sets from its own authentication. The role is also written,
  root-owned, to `/run/sosa/turn.json` before each turn, so a stack's tool that is the owner's call can refuse
  anyone else in code, whatever the agent was told.
- **The keeper** (`app`). `app load` builds the app, starts the new build beside the live one, waits for health,
  points the stable port at it, stops the old one. If the new one won't come up, the live one stays. The last
  load that stayed up is remembered; `app rollback` puts it back.
- **The changes history** (`changes`). Git over `/` with a path list. The harness commits after every turn,
  named for whose turn it was; the agent commits its own finished work. `changes log --stat` says what changed
  since it last worked.
- **Snapshots.** `sosa snapshot NAME` copies the whole disk (paused for a second); `sosa rollback` puts one back.
- **The preview.** Each of the app's ports is on the host at `127.0.0.1:<port>`, over vsock. Put that behind
  whatever serves your users.
- **Metering.** The relay counts every answer's tokens per box, in `~/.sosa/usage.jsonl`, and stops a box at
  its daily budget (`SOSA_BOX_DAY_USD`, default 20) or everyone at theirs (`SOSA_DAY_USD`, default 100).

## The daemon and its API

`sosad` owns the boxes. The CLI is a thin client; so is your backend. With an address in `~/.sosa/tcp`
(or `sosa daemon --tcp 127.0.0.1:8790`) it listens on TCP too, with a bearer token in `~/.sosa/api-token`.
Only one daemon runs per home (a lock); the CLI starts it when it isn't up. The routes are listed at the top of
[sosa/daemon.py](sosa/daemon.py); the shape is one box per user, created from a stack, started when wanted,
`POST /v1/boxes/{name}/turn` streaming NDJSON (`facts`, `delta`, `tool`, `done` or `error`), and the preview
ports to put behind your own proxy. Boxes that were up come back when the daemon restarts.

## The front door

`sosa front` is one address for every box: `http://NAME.ZONE/` is box NAME's app, `http://NAME-PORT.ZONE/` one
of its other ports. A box that is asleep or down is started on the first request, which waits (about eight
seconds from a baked image) instead of failing. A box with an idle timer (`sosa create --idle 30`, `sosa idle
NAME 30`) is stopped and marked asleep after that many minutes without a turn, an exec or a request, so a host
can hold far more boxes than it can run.

Hosts announce themselves. On each host, `sosa join http://front:8180 --name host-1` (it asks for the front
door's register secret) makes the daemon report its address, token, boxes, stacks, images and free memory every
30 seconds and whenever a box changes. The front door keeps the registry on disk, routes `NAME.ZONE` to whichever
host has NAME, and refuses a name two hosts claim. The preview rides inside the daemon's API
(`POST /v1/boxes/NAME/tunnel`), so a host's preview ports never leave its loopback.

That makes the front door the one address for a backend too: `GET /_sosa/api/v1/boxes` lists every host's
boxes, `POST /_sosa/api/v1/boxes` creates one on the host with the stack's image and the fewest boxes running (or
on `"host"`), and `/_sosa/api/v1/boxes/NAME/...` is the daemon's own route for that box, streamed through, on
whichever host has it. Put your own login in front: with a `secret_file` in `front.json`, only requests carrying
`X-Sosa-Front: <secret>` get in, and `X-Sosa-User` from your proxy is passed to the box's app. The routes and
the config are at the top of [sosa/front.py](sosa/front.py).

## Stacks

Yours are private. This repo ships `stacks/hello` to prove the contract. `sosa stack init DIR` writes a
skeleton; `sosa stack check NAME` tells you whether sosa will run it. The contract, the bake, the gate, the
facts: [docs/CONTRACT.md](docs/CONTRACT.md). An agent can write a stack: point one at this repo and your
templates, with the contract as the brief.

## For agents using the CLI

[SKILL.md](SKILL.md) is the page to give an agent that will drive sosa. Every command prints its help at the
top of its file, errors say what to do next, `--json` gives the daemon's answer as it came, and the commands are
idempotent where they can be.

## Where it came from

sosa is the generic layer extracted from several earlier agent-in-a-box projects. Each one hardened the wall
and handed the agent more because of it. What was particular to each is a stack; what they shared is here.

## Runtime state

`~/.sosa/` (0700): `bin/`, `images/` (the base image and baked stack images), `stacks/`, `boxes/<name>/`
(`box.json`, `disk.raw`, the seed and payload disks, `serial.log`, `egress.log`, `snapshots/`, `prompt.md`,
`transcripts.jsonl`), `anthropic-key`, `usage.jsonl`, `sosad.sock`, `sosad.log`.

## License

MIT.
