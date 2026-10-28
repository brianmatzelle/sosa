#!/usr/bin/env python3
"""The host's idle stop, run by the sosa-host-idle timer every minute as root (aws/bootstrap.sh installs it).

Once no box has been running for IDLE_MINUTES (/etc/sosa/aws.env) and nobody is in an SSM session, the host
powers itself off, which stops the instance (InstanceInitiatedShutdownBehavior: stop). The front door starts it
again on the next request for one of its boxes (aws/wake.sh). The clock starts at boot, so whoever started the
host has the whole wait to use it. Boxes with their own idle timer go to sleep first; a box with none keeps the
host up. State: /var/lib/sosa/host-idle.json.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

ENV = "/etc/sosa/aws.env"
STATE = "/var/lib/sosa/host-idle.json"
SOCK = "/home/sosa/.sosa/sosad.sock"


def env():
    out = {}
    try:
        for ln in open(ENV):
            k, _, v = ln.strip().partition("=")
            if k and not k.startswith("#"):
                out[k] = v.strip('"')
    except OSError:
        pass
    return out


def boxes_up():
    """How many boxes the daemon has running; None when the daemon isn't answering (counts as busy: it may be
    starting)."""
    class U(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(SOCK)
    try:
        c = U("sosad")
        c.request("GET", "/v1/health")
        r = c.getresponse()
        d = json.loads(r.read())
        return int(d.get("boxes_up") or 0)
    except (OSError, ValueError):
        return None


def ssm_session():
    r = subprocess.run(["pgrep", "-f", "ssm-session-worker"], capture_output=True)
    return r.returncode == 0


def main():
    e = env()
    idle = int(e.get("IDLE_MINUTES") or 0)
    if idle <= 0:
        return
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    try:
        st = json.load(open(STATE))
        if st.get("boot", 0) < boot - 60:     # a new boot: the clock starts again
            st = {}
    except (OSError, ValueError):
        st = {}
    last = st.get("active_at") or boot
    up = boxes_up()
    why = None
    if up is None:
        why = "daemon not answering"
    elif up > 0:
        why = f"{up} box(es) running"
    elif ssm_session():
        why = "an SSM session"
    now = time.time()
    if why:
        last = now
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"boot": boot, "active_at": last, "busy": why, "checked": now}, f)
    if now - last >= idle * 60:
        print(f"idle for {idle} min: powering off (the front door wakes this host on the next request)")
        sys.stdout.flush()
        subprocess.run(["systemctl", "poweroff"])


if __name__ == "__main__":
    main()
