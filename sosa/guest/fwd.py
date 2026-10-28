#!/usr/bin/env python3
"""fwd.py: the box's only openings, inside the VM (sosa-fwd.service). The VM has no network interface; every
byte in or out goes over vsock to the host (CID 2), where cloud-hypervisor hands it to a unix socket sosad holds.

Out (guest TCP -> host vsock port):
    127.0.0.1:3128 -> host 3128   the egress proxy (sosa/egress.py): CONNECT to the hosts the stack allows
    127.0.0.1:8097 -> host 8097   the relay (sosa/relay.py): Claude, with the key held on the host
In (host vsock port -> guest TCP), the preview path:
    vsock 5001                    the host sends "PORT <n>\\n" first; the rest is bridged to 127.0.0.1:<n>
                                  (a port from the stack's list, checked against /etc/sosa/ports)
TLS stays end to end; nothing here or on the host sees inside it.
"""
import os
import re
import socket
import sys
import threading
import time

HOST_CID = 2
OUT = {3128: 3128, 8097: 8097}          # local port -> host vsock port
PREVIEW_PORT = 5001
PORTS_FILE = "/etc/sosa/ports"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


def pump(src, dst):
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def bridge(a, b):
    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join()
    a.close()
    b.close()


def out_handle(c, host_port):
    try:
        v = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        v.settimeout(10)
        v.connect((HOST_CID, host_port))
        v.settimeout(None)
    except OSError as e:
        log(f"host port {host_port} unreachable: {e}")
        c.close()
        return
    bridge(c, v)


def out_listener(local, host_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", local))
    s.listen(64)
    log(f"127.0.0.1:{local} -> host vsock {host_port}")
    while True:
        c, _ = s.accept()
        threading.Thread(target=out_handle, args=(c, host_port), daemon=True).start()


def allowed_ports():
    try:
        return {int(x) for x in open(PORTS_FILE).read().split() if x.isdigit()}
    except OSError:
        return set()


def preview_handle(v, peer):
    try:
        if peer and peer[0] != HOST_CID:
            log(f"preview: refused cid {peer[0]}")
            return v.close()
        v.settimeout(10)
        line = b""
        while not line.endswith(b"\n") and len(line) < 64:
            ch = v.recv(1)
            if not ch:
                return v.close()
            line += ch
        m = re.fullmatch(rb"PORT (\d{1,5})\n", line)
        if not m:
            v.sendall(b"ERR expected PORT <n>\n")
            return v.close()
        port = int(m.group(1))
        if port not in allowed_ports():
            v.sendall(f"ERR port {port} is not in {PORTS_FILE}\n".encode())
            return v.close()
        try:
            t = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError as e:
            v.sendall(f"ERR nothing on 127.0.0.1:{port}: {e}\n".encode())
            return v.close()
        v.sendall(b"OK\n")
        v.settimeout(None)
        t.settimeout(None)
        bridge(v, t)
    except OSError as e:
        log(f"preview: {e}")
        try:
            v.close()
        except OSError:
            pass


def preview_listener():
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.bind((socket.VMADDR_CID_ANY, PREVIEW_PORT))
    s.listen(64)
    log(f"vsock {PREVIEW_PORT} -> 127.0.0.1:<port in {PORTS_FILE}>")
    while True:
        v, peer = s.accept()
        threading.Thread(target=preview_handle, args=(v, peer), daemon=True).start()


def main():
    for local, host_port in OUT.items():
        threading.Thread(target=out_listener, args=(local, host_port), daemon=True).start()
    preview_listener()


if __name__ == "__main__":
    main()
