---
name: sosa
description: Drive sosa, the agent-in-a-box engine: make boxes from stacks, start and stop them, send turns, read status, bake images, check stacks. Use when asked to create or operate a sosa box, write or test a stack, or wire a backend to sosad.
---

# sosa, for an agent at the keyboard

`sosa` is a CLI over `sosad`, a local daemon that owns microVM "boxes", each with its own Claude Code agent
inside. The daemon starts itself on the first command. Every command accepts `--json` (put it first:
`sosa --json status t1`), and exits non-zero with a one-line reason on stderr that says what to do next.

## Rules

- A box is named like a hostname: `[a-z][a-z0-9-]*`. A stack is a directory with `stack.toml`; the names of its
  files are the contract (`docs/CONTRACT.md`). Never edit `~/.sosa/stacks/*` in place; edit the source dir and
  `sosa stack add DIR` again (it replaces the copy), then `sosa push NAME` to a running box.
- Lifecycle: `create` (seconds) → `start` (first boot ~15 s, up to 4 min from a bake) → turns → `stop` (the
  ACPI button; waits for a clean shutdown) → `destroy` (asks; `-f` doesn't). `start` is idempotent.
- Turns are serialized per box. `sosa ask NAME "..."` streams the reply; a turn can take minutes. Pass
  `--who` and `--role owner|guest` truthfully: the box's gate reads the role.
- Before anything risky inside a box, `sosa snapshot NAME label`. `sosa rollback NAME SNAPSHOT` undoes it.
- `sosa exec NAME -- CMD...` runs as root in the box; `--user sosa` as the agent. Use it to look, not to work
  around the agent.
- Secrets: `sosa key` and `sosa token NAME` read from a hidden prompt and need a real terminal. Never pass a key
  on the command line or print one.

## The usual sequences

```sh
# a stack from scratch
sosa stack init ./my-stack --name my-stack      # then fill stack.toml, brief.md, seed/, tools/, bake.sh
sosa stack add ./my-stack                       # copies it in and checks the files
sosa stack check my-stack --quick               # files only
sosa stack check my-stack                       # bake + a box + app load + one turn (minutes)

# boxes
sosa create t1 --stack my-stack --owner alice --start
sosa status t1                                  # running? mind? app healthy? preview URL?
sosa ask t1 --who alice --role owner "add a /version endpoint and load it"
sosa preview t1                                 # 127.0.0.1:<port> -> the app's stable port
sosa exec t1 --user sosa --cwd / -- changes log --stat -3
sosa transcript t1 -n 5

# when something is off
sosa console t1 -n 80                           # the serial log (boot problems)
sosa exec t1 -- journalctl -u sosa-mind -n 50 --no-pager      # the mind
sosa exec t1 -- app status && sosa exec t1 -- app log 50      # the app
sosa cancel t1                                  # a stuck turn
sosa new t1                                     # a fresh conversation (memory files stay)
tail -50 ~/.sosa/sosad.log                      # the daemon
cat ~/.sosa/boxes/t1/egress.log                 # what the box tried to reach (REFUSED lines)
```

## Reading status

`sosa --json status NAME` gives `running`, `mind` (`claude` version, `auth` relay|token|None, `session`,
`busy`), `app` (`live.slot/port`, `healthy`, `good` rev), `preview` (`{"8080": "http://127.0.0.1:18000"}`),
`egress` counts. `auth: None` means the box can't think: `sosa key` (relay) or `sosa token NAME`.

## The front door

`sosa front --zone example.com` serves `http://NAME.example.com/` for every box, waking one that is asleep.
`sosa idle NAME 30` makes a box sleep after 30 untouched minutes; `sosa list` shows `asleep`. A request, a
turn or `sosa start` wakes it. `http://example.com/_sosa/boxes` lists the routes. A host joins a front door
with `sosa join URL --name H` (register secret asked, hidden; `--secret-file F` without a terminal);
`sosa join --status` says whether the last report was accepted; `sosa hosts` lists every host there. A backend
can use the front door alone: `/_sosa/api/v1/boxes` (GET all, POST creates on the best host) and
`/_sosa/api/v1/boxes/NAME/...` (the daemon's routes, on the right host).

## The API, for a backend

Same verbs over HTTP: unix socket `~/.sosa/sosad.sock`, or `sosa daemon --tcp 127.0.0.1:8790` with
`Authorization: Bearer $(cat ~/.sosa/api-token)`. `POST /v1/boxes/{name}/turn` with
`{"text","who","role","channel"}` streams NDJSON events; the rest is JSON. The list is at the top of
`sosa/daemon.py`.
