"""box.py: a box is a cloud-hypervisor microVM with no network interface. This makes, runs, reaches and keeps one.

Runtime state, all under SOSA_HOME (~/.sosa, 0700):
    bin/                 cloud-hypervisor, ch-remote, the firmware (install.sh)
    images/noble-<arch>.raw          the Ubuntu cloud image; images/<stack>.raw + .json  a stack's baked image
    stacks/<name>/       stacks sosa knows (copies: `sosa stack add`)
    boxes/<name>/        box.json, disk.raw, seed.iso, payload.img, serial.log, ch.sock, vsock, vsock_3128 (egress),
                         vsock_8097 (relay), egress.log, snapshots/, prompt.md, transcripts.jsonl, token
The guest is reached over hybrid vsock: `vsock` is cloud-hypervisor's unix socket; "CONNECT 5000" reaches mind.py.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUEST = os.path.join(HERE, "guest")
sys.path.insert(0, GUEST)
import mind as mindpy  # noqa: E402  the client half of the guest's mind.py

PY = "/usr/bin/python3"                 # the same 3.12 the guest runs; -I everywhere
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MIND_PORT, PREVIEW_PORT = 5000, 5001
# Units that only wait for or use a network. Masked in the disk before the first boot: with no NIC,
# systemd-networkd-wait-online alone holds cloud-init (cloud-config wants network-online.target) for 2 min.
MASK = ("systemd-networkd-wait-online.service pollinate.service snapd.service snapd.socket snapd.seeded.service "
        "snapd.apparmor.service snapd.autoimport.service snapd.core-fixup.service snapd.recovery-chooser-trigger.service "
        "unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer motd-news.timer fwupd-refresh.timer "
        "ModemManager.service open-vm-tools.service ubuntu-advantage.service ua-reboot-cmds.service esm-cache.service "
        "systemd-timesyncd.service ssh.service ssh.socket lxd-installer.socket").split()


class BoxError(Exception):
    pass


def arch():
    m = platform.machine()
    if m == "aarch64":
        return "arm64", "CLOUDHV_EFI.fd"
    if m == "x86_64":
        return "amd64", "CLOUDHV.fd"
    raise BoxError(f"unsupported host {m}")


class Home:
    def __init__(self, path=None):
        self.path = os.path.abspath(os.path.expanduser(path or os.environ.get("SOSA_HOME") or "~/.sosa"))
        self.bin = os.path.join(self.path, "bin")
        self.images = os.path.join(self.path, "images")
        self.stacks = os.path.join(self.path, "stacks")
        self.boxes = os.path.join(self.path, "boxes")
        self.arch, self.firmware = arch()
        self.base_image = os.path.join(self.images, f"noble-{self.arch}.raw")
        self.claude = os.path.realpath(os.path.expanduser(os.environ.get("SOSA_CLAUDE") or "~/.local/bin/claude"))

    def ensure(self):
        for d in (self.path, self.bin, self.images, self.stacks, self.boxes):
            os.makedirs(d, exist_ok=True)
        os.chmod(self.path, 0o700)

    def tool(self, name):
        p = os.path.join(self.bin, name)
        if not os.path.exists(p):
            raise BoxError(f"missing {p} (install.sh)")
        return p

    def check_tools(self):
        for t in ("xorriso", "mke2fs", "debugfs", "sfdisk"):
            if not shutil.which(t):
                raise BoxError(f"needs {t} on the host")
        self.tool("cloud-hypervisor")
        self.tool("ch-remote")
        self.tool(self.firmware)
        if not os.path.exists(self.base_image):
            raise BoxError(f"no base image at {self.base_image} (install.sh)")
        if not os.access(self.claude, os.X_OK):
            raise BoxError(f"no Claude Code at {self.claude} to copy into the box (SOSA_CLAUDE=...)")

    def stack_image(self, stack):
        p = os.path.join(self.images, f"{stack}.raw")
        return p if os.path.exists(p) else None

    def names(self):
        try:
            return sorted(n for n in os.listdir(self.boxes) if NAME_RE.match(n)
                          and os.path.exists(os.path.join(self.boxes, n, "box.json")))
        except FileNotFoundError:
            return []


def root_offset(image):
    """Byte offset of the Linux root partition in a raw disk image."""
    out = subprocess.run(["sfdisk", "-J", image], capture_output=True, text=True, check=True).stdout
    t = json.loads(out)["partitiontable"]
    p = [p for p in t["partitions"] if p["type"].upper().startswith("0FC63DAF")]
    if not p:
        raise BoxError(f"no Linux root partition in {image}")
    return p[0]["start"] * t.get("sectorsize", 512)


def iec(s):
    return int(subprocess.run(["numfmt", "--from=iec", str(s)], capture_output=True, text=True, check=True).stdout)


class Box:
    def __init__(self, home, name):
        if not NAME_RE.match(name):
            raise BoxError(f"a box is named like a hostname: [a-z][a-z0-9-]*, not {name!r}")
        self.home = home
        self.name = name
        self.dir = os.path.join(home.boxes, name)
        self.conf_path = os.path.join(self.dir, "box.json")
        self.disk = os.path.join(self.dir, "disk.raw")
        self.seed = os.path.join(self.dir, "seed.iso")
        self.payload = os.path.join(self.dir, "payload.img")
        self.serial = os.path.join(self.dir, "serial.log")
        self.ch_sock = os.path.join(self.dir, "ch.sock")
        self.vsock = os.path.join(self.dir, "vsock")
        self.egress_sock = self.vsock + "_3128"
        self.relay_sock = self.vsock + "_8097"
        self.egress_log = os.path.join(self.dir, "egress.log")
        self.snapshots = os.path.join(self.dir, "snapshots")
        self.token_file = os.path.join(self.dir, "token")
        self.prompt_file = os.path.join(self.dir, "prompt.md")
        self.transcripts = os.path.join(self.dir, "transcripts.jsonl")
        self._conf = None

    # -- config
    @property
    def conf(self):
        if self._conf is None:
            try:
                with open(self.conf_path) as f:
                    self._conf = json.load(f)
            except FileNotFoundError:
                raise BoxError(f"no box {self.name!r} (sosa create {self.name} --stack <stack>)")
        return self._conf

    def save_conf(self, c):
        self._conf = c
        tmp = self.conf_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f, indent=2)
        os.replace(tmp, self.conf_path)

    def exists(self):
        return os.path.exists(self.conf_path)

    @property
    def target(self):
        return f"hvsock:{self.vsock}:{MIND_PORT}"

    # -- make
    def create(self, stack, image=None, mem=None, cpus=None, disk=None, auth="relay", owner="", extra_env=None,
               cid=None, bake=False, preview_port=None):
        if os.path.exists(self.dir) and os.listdir(self.dir) not in ([], ["token"]):
            raise BoxError(f"{self.dir} exists (sosa destroy {self.name} first)")
        self.home.check_tools()
        image = image or self.home.stack_image(stack.name) or self.home.base_image
        mem = mem or stack.box["mem"]
        cpus = int(cpus or stack.box["cpus"])
        disk = disk or stack.box["disk"]
        if iec(disk) < os.stat(image).st_size:
            raise BoxError(f"disk {disk} is smaller than the image {image}")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        part = self.disk + ".part"
        subprocess.run(["cp", "--sparse=always", image, part], check=True)
        subprocess.run(["truncate", "-s", disk, part], check=True)      # cloud-init grows the root fs into it
        if image == self.home.base_image:
            self._mask_units(part)
        conf = {"name": self.name, "stack": stack.name, "stack_path": stack.path, "image": image, "mem": mem,
                "cpus": cpus, "disk": disk, "cid": cid or 3, "auth": auth, "owner": owner, "bake": bake,
                "preview_port": preview_port, "ports": list(stack.app.get("ports") or []),
                "agent": stack.agent, "created": time.strftime("%FT%T")}
        self.save_conf(conf)
        self._render_seed(stack, extra_env)
        self._make_payload(stack, extra_env)
        os.replace(part, self.disk)
        return conf

    def _mask_units(self, part):
        off = root_offset(part)
        script = "".join(f"symlink /etc/systemd/system/{u} /dev/null\n" for u in MASK)
        p = subprocess.run(["debugfs", "-w", "-f", "-", f"{part}?offset={off}"], input=script, capture_output=True,
                           text=True)
        chk = subprocess.run(["debugfs", "-R", "stat /etc/systemd/system/systemd-networkd-wait-online.service",
                              f"{part}?offset={off}"], capture_output=True, text=True)
        if "Type: symlink" not in chk.stdout:
            raise BoxError(f"masking units in the disk didn't take: {p.stderr[-500:]}")

    def _render_seed(self, stack, extra_env):
        c = self.conf
        tz = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True,
                            text=True).stdout.strip() or "UTC"
        t = open(os.path.join(GUEST, "user-data.tmpl")).read()
        a = stack.agent
        sudo_line = "    sudo: ALL=(ALL) NOPASSWD:ALL" if a.get("sudo") else "    sudo: false"
        for k, v in {"__HOSTNAME__": self.name, "__TZ__": tz, "__USER__": a["user"], "__CWD__": a["cwd"],
                     "__APP_DIR__": stack.app["dir"], "__TIMEOUT__": str(int(a["timeout_s"])),
                     "__SUDO_LINE__": sudo_line}.items():
            t = t.replace(k, v)
        with open(os.path.join(self.dir, "user-data"), "w") as f:
            f.write(t)
        shutil.copy(os.path.join(GUEST, "network-config"), os.path.join(self.dir, "network-config"))
        # a new instance-id per create, so cloud-init treats a re-created disk (or a baked image) as a first boot
        with open(os.path.join(self.dir, "meta-data"), "w") as f:
            f.write(f"instance-id: {self.name}-{time.strftime('%Y%m%d%H%M%S')}\nlocal-hostname: {self.name}\n")
        subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-o", self.seed, "-V", "cidata", "-J", "-r",
                        os.path.join(self.dir, "user-data"), os.path.join(self.dir, "meta-data"),
                        os.path.join(self.dir, "network-config")], check=True, capture_output=True)

    def _make_payload(self, stack, extra_env):
        """The payload disk: an ext4 image (mke2fs -d) with what the guest can't download."""
        stage = os.path.join(self.dir, "payload.d")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(os.path.join(stage, "bin"))
        os.makedirs(os.path.join(stage, "etc"))
        shutil.copy(self.home.claude, os.path.join(stage, "claude"))
        for n in ("mind.py", "fwd.py", "changes", "sosa-app"):
            shutil.copy(os.path.join(GUEST, n), os.path.join(stage, "bin", n))
        shutil.copytree(stack.path, os.path.join(stage, "stack"), symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        if os.path.isdir(os.path.join(stack.path, "home")):
            shutil.copytree(os.path.join(stack.path, "home"), os.path.join(stage, "home"))
        with open(os.path.join(stage, "etc", "env"), "w") as f:
            f.write(stack.env_lines({"SOSA_BOX": self.name, **(extra_env or {})}))
        with open(os.path.join(stage, "etc", "ports"), "w") as f:
            f.write(" ".join(str(p) for p in stack.app.get("ports") or []) + "\n")
        if stack.has_app:
            with open(os.path.join(stage, "etc", "app.json"), "w") as f:
                json.dump(stack.app_json(), f)
        size = int(subprocess.run(["du", "-sm", stage], capture_output=True, text=True).stdout.split()[0]) + 64
        try:
            os.unlink(self.payload)
        except FileNotFoundError:
            pass
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-L", "sosaload", "-E", "root_owner=0:0", "-d", stage,
                        self.payload, f"{size}M"], check=True, capture_output=True)
        shutil.rmtree(stage)

    # -- run
    def running(self):
        if not os.path.exists(self.ch_sock):
            return False
        r = subprocess.run([self.home.tool("ch-remote"), "--api-socket", self.ch_sock, "ping"], capture_output=True)
        return r.returncode == 0

    def launch(self, egress_allow, allow_all=False, allow_plain=False):
        """Start the egress proxy and cloud-hypervisor; returns both Popens. The caller supervises them."""
        c = self.conf
        if self.running():
            raise BoxError(f"{self.name} is already running")
        if not os.path.exists(self.disk):
            raise BoxError(f"{self.name} has no disk (sosa create)")
        for p in (self.ch_sock, self.vsock, self.egress_sock):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        if os.path.exists(self.serial):
            os.replace(self.serial, self.serial + ".1")
        old = os.umask(0o077)
        try:
            eg_cmd = [PY, "-I", os.path.join(HERE, "egress.py"), "--listen", self.egress_sock, "--log", self.egress_log]
            for h in egress_allow:
                eg_cmd += ["--allow", h]
            if allow_all:
                eg_cmd.append("--allow-all")
            if allow_plain:
                eg_cmd.append("--allow-plain")
            egress = subprocess.Popen(eg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if os.path.exists(self.egress_sock):
                    break
                time.sleep(0.1)
            disks = [f"path={self.disk},image_type=raw", f"path={self.seed},readonly=on,sparse=off,image_type=raw"]
            if os.path.exists(self.payload):
                disks.append(f"path={self.payload},readonly=on,sparse=off,image_type=raw")
            cmd = [self.home.tool("cloud-hypervisor"), "--api-socket", f"path={self.ch_sock}",
                   "--cpus", f"boot={c['cpus']}", "--memory", f"size={c['mem']}",
                   "--balloon", "size=0,free_page_reporting=on",
                   "--firmware", self.home.tool(self.home.firmware),
                   "--disk", *disks,
                   "--vsock", f"cid={c['cid']},socket={self.vsock}",
                   "--serial", f"file={self.serial}", "--console", "off"]
            ch = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(os.path.join(self.dir, "ch.err"), "ab"))
        finally:
            os.umask(old)
        return ch, egress

    def power_button(self):
        subprocess.run([self.home.tool("ch-remote"), "--api-socket", self.ch_sock, "power-button"], capture_output=True)

    def stop(self, timeout=120):
        """The ACPI power button, then wait for a clean guest shutdown; force after timeout."""
        if not self.running():
            return "not running"
        self.power_button()
        for _ in range(int(timeout)):
            if not self.running():
                return "stopped"
            time.sleep(1)
        subprocess.run([self.home.tool("ch-remote"), "--api-socket", self.ch_sock, "shutdown-vmm"], capture_output=True)
        return "forced"

    def cleanup_sockets(self):
        for p in (self.ch_sock, self.vsock, self.egress_sock):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    # -- reach
    def health(self, timeout=5):
        try:
            return mindpy.one(self.target, {"cmd": "health"}, timeout)
        except (OSError, ConnectionError, ValueError, RuntimeError):
            return None

    def wait_mind(self, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            h = self.health(3)
            if h and h.get("ok"):
                return h
            time.sleep(1)
        raise BoxError(f"{self.name}: the mind didn't answer within {timeout:g} s (sosa console {self.name})")

    def exec(self, argv, timeout=120, user=None, cwd=None, stdin=None, env=None):
        req = {"cmd": "exec", "argv": argv, "timeout": timeout}
        if user:
            req["user"] = user
        if cwd:
            req["cwd"] = cwd
        if env:
            req["env"] = env
        if stdin is not None:
            import base64
            req["stdin_b64"] = base64.b64encode(stdin if isinstance(stdin, bytes) else stdin.encode()).decode()
        return mindpy.one(self.target, req, timeout + 30)

    def sh(self, script, timeout=120, user=None, cwd=None):
        return self.exec(["bash", "-c", script], timeout, user, cwd)

    def send_file(self, local, remote, mode="0644", owner="root"):
        return mindpy.send_file(self.target, local, remote, mode, owner)

    def put_text(self, remote, text, mode="0644", owner="root"):
        import base64
        ev = mindpy.one(self.target, {"cmd": "put", "path": remote, "b64": base64.b64encode(text.encode()).decode(),
                                      "mode": mode, "owner": owner})
        if not ev.get("ok"):
            raise BoxError(f"put {remote}: {ev}")

    def set_token(self, token):
        ev = mindpy.one(self.target, {"cmd": "token", "token": token})
        if not ev.get("ok"):
            raise BoxError(f"the box refused the token: {ev.get('error')}")

    def push(self, stack=None, claude=False):
        """Ship the guest programs (and the stack) into a running box; restart what changed."""
        if not self.running():
            raise BoxError(f"{self.name} is not running")
        h = self.health()
        if not h:
            raise BoxError(f"{self.name}: the mind isn't answering")
        out = []
        self.send_file(os.path.join(GUEST, "fwd.py"), "/opt/sosa/bin/fwd.py", "0755")
        self.send_file(os.path.join(GUEST, "changes"), "/opt/sosa/bin/changes", "0755")
        self.send_file(os.path.join(GUEST, "sosa-app"), "/opt/sosa/bin/sosa-app", "0755")
        self.sh("systemctl restart sosa-fwd.service")
        want = subprocess.run([self.home.claude, "--version"], capture_output=True, text=True).stdout.split()[0]
        if claude or h.get("claude") != want:
            self.send_file(self.home.claude, "/usr/local/bin/claude", "0755")
            out.append(f"Claude Code {h.get('claude') or '?'} -> {want}")
        if stack is not None:
            # the stack, whole: a tarball through put, unpacked over /opt/sosa/stack
            tar = os.path.join(self.dir, "stack.tgz")
            subprocess.run(["tar", "-czf", tar, "--exclude=.git", "--exclude=__pycache__", "-C", stack.path, "."], check=True)
            self.send_file(tar, "/opt/sosa/stack.tgz")
            os.unlink(tar)
            self.sh("rm -rf /opt/sosa/stack.new && mkdir -p /opt/sosa/stack.new && tar -xzf /opt/sosa/stack.tgz -C /opt/sosa/stack.new "
                    "&& rm -rf /opt/sosa/stack.old && mv /opt/sosa/stack /opt/sosa/stack.old && mv /opt/sosa/stack.new /opt/sosa/stack "
                    "&& chmod -R a+rX /opt/sosa/stack && rm -f /opt/sosa/stack.tgz")
            self.put_text("/etc/sosa/env", stack.env_lines({"SOSA_BOX": self.name}))
            self.put_text("/etc/sosa/ports", " ".join(str(p) for p in stack.app.get("ports") or []) + "\n")
            if stack.has_app:
                self.put_text("/etc/sosa/app.json", json.dumps(stack.app_json()))
            out.append("stack updated")
        # the new mind goes in beside the old one; the old one is kept for sosa-mind-rollback.service
        self.send_file(os.path.join(GUEST, "mind.py"), "/opt/sosa/bin/mind.py.new", "0755")
        self.sh("cp -p /opt/sosa/bin/mind.py /opt/sosa/bin/mind.py.prev && mv -f /opt/sosa/bin/mind.py.new /opt/sosa/bin/mind.py")
        try:
            # the mind restarting itself: the connection closes before it can answer, which is fine
            self.exec(["systemctl", "--no-block", "restart", "sosa-mind.service"], 10)
        except (OSError, ConnectionError, ValueError):
            pass
        import hashlib
        want_d = hashlib.sha256(open(os.path.join(GUEST, "mind.py"), "rb").read()).hexdigest()[:12]
        for _ in range(30):
            time.sleep(1)
            h = self.health(3)
            if h and h.get("mind") == want_d:
                out.append(f"mind {want_d} is up")
                return out
        raise BoxError(f"the new mind ({want_d}) didn't come up in 30 s; sosa exec {self.name} journalctl -u sosa-mind")

    # -- keep
    def snapshot(self, label=""):
        if label and not re.fullmatch(r"[a-z0-9-]{1,40}", label):
            raise BoxError("a label is [a-z0-9-]")
        os.makedirs(self.snapshots, exist_ok=True)
        snap = os.path.join(self.snapshots, time.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "") + ".raw")
        was = self.running()
        if was:
            try:
                self.exec(["sync"], 30)
            except Exception:  # noqa: BLE001  crash-consistent then
                pass
            subprocess.run([self.home.tool("ch-remote"), "--api-socket", self.ch_sock, "pause"], capture_output=True)
        try:
            subprocess.run(["cp", "--sparse=always", self.disk, snap + ".part"], check=True)
            os.replace(snap + ".part", snap)
        finally:
            if was:
                subprocess.run([self.home.tool("ch-remote"), "--api-socket", self.ch_sock, "resume"], capture_output=True)
        keep = int(os.environ.get("SOSA_SNAP_KEEP") or 5)
        for old in sorted(self.list_snapshots(), key=lambda s: s["name"], reverse=True)[keep:]:
            os.unlink(os.path.join(self.snapshots, old["name"] + ".raw"))
        return os.path.basename(snap)[:-4]

    def list_snapshots(self):
        out = []
        try:
            for n in sorted(os.listdir(self.snapshots)):
                if n.endswith(".raw"):
                    p = os.path.join(self.snapshots, n)
                    out.append({"name": n[:-4], "bytes": os.stat(p).st_blocks * 512})
        except FileNotFoundError:
            pass
        return out

    def rollback_disk(self, name):
        """Replace the disk with a snapshot. The box must be stopped; the old disk becomes a snapshot itself."""
        snap = os.path.join(self.snapshots, name.removesuffix(".raw") + ".raw")
        if not os.path.exists(snap):
            raise BoxError(f"no snapshot {name}")
        if self.running():
            raise BoxError("stop the box first")
        os.replace(self.disk, os.path.join(self.snapshots, time.strftime("%Y%m%d-%H%M%S") + "-before-rollback.raw"))
        subprocess.run(["cp", "--sparse=always", snap, self.disk + ".part"], check=True)
        os.replace(self.disk + ".part", self.disk)

    def destroy(self, keep_token=True):
        if self.running():
            self.stop()
        for n in os.listdir(self.dir) if os.path.isdir(self.dir) else []:
            if keep_token and n == "token":
                continue
            p = os.path.join(self.dir, n)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
        if not os.listdir(self.dir):
            os.rmdir(self.dir)
        self._conf = None

    def disk_usage(self):
        try:
            st = os.stat(self.disk)
            return {"apparent": st.st_size, "used": st.st_blocks * 512}
        except FileNotFoundError:
            return None

    def egress_counts(self):
        a = r = 0
        last = ""
        try:
            with open(self.egress_log) as f:
                for line in f:
                    if " ALLOWED " in line:
                        a += 1
                    elif " REFUSED " in line:
                        r += 1
                        last = line.strip()
        except FileNotFoundError:
            pass
        return {"allowed": a, "refused": r, "last_refused": last}
