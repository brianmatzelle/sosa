# Where you are

A small box made from the `hello` stack: a web app in `/srv/app`, written in Python with the standard library only
(there is no pip here and no network to fetch anything with). `app.py` is the server; it listens on `$PORT`.

- `app load` builds it (`tools/build`, a syntax check) and swaps it in live behind the stable port 8080, which the
  people outside see as the preview. `app status` and `app log` say how it's doing. `curl http://127.0.0.1:8080/`
  reaches the live one.
- `tools/health` is what the keeper asks before it swaps: `GET /health` on the new instance must answer 200.
- `tools/deploy` is a privileged tool: it refuses unless the turn's header says OWNER.
- Keep the app one file unless there's a reason not to. Say what you changed.
