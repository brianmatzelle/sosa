#!/usr/bin/env bash
# install.sh: one-time host setup for sosa (safe to re-run; every step skips what's done).
#
#   ./install.sh            # or: sosa install
#
# 1. ~/.sosa/bin: cloud-hypervisor, ch-remote, the EFI firmware (pinned versions)
# 2. ~/.sosa/images/noble-<arch>.raw: Ubuntu 24.04 cloud image, checksum-verified, converted to raw
#    (qemu-img if it's here, else in a throwaway container)
# 3. ~/.config/systemd/user/sosad.service, so the daemon starts at login (or at boot, with linger)
# Needs: /dev/kvm readable by you, python3 (3.11+), curl, xorriso, mke2fs, debugfs, sfdisk, and Claude Code at
# ~/.local/bin/claude (it's copied into every box).
set -euo pipefail
REPO=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
HOME_DIR=${SOSA_HOME:-$HOME/.sosa}
CH_VERSION=v53.0
EDK2_TAG=ch-97eeb7b09
CLOUD_URL=https://cloud-images.ubuntu.com/noble/current
case $(uname -m) in
  aarch64) ARCH=arm64; CH_SUFFIX=-aarch64; FIRMWARE=CLOUDHV_EFI.fd ;;
  x86_64)  ARCH=amd64; CH_SUFFIX="";       FIRMWARE=CLOUDHV.fd ;;
  *) echo "no pinned binaries for $(uname -m)" >&2; exit 1 ;;
esac
CLOUD_IMG=noble-server-cloudimg-$ARCH.img
step() { printf '\n== %s\n' "$*"; }
fetch() { [[ -s $2 ]] && return 0; echo "  downloading $(basename "$2")"; curl -fsSL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"; }

[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "/dev/kvm isn't usable by $USER: add yourself to the kvm group first" >&2; exit 1; }
for t in curl xorriso mke2fs debugfs sfdisk /usr/bin/python3; do command -v "$t" >/dev/null || { echo "missing $t" >&2; exit 1; }; done
/usr/bin/python3 -c 'import tomllib' 2>/dev/null || { echo "/usr/bin/python3 must be 3.11+ (tomllib)" >&2; exit 1; }
[[ -x ${SOSA_CLAUDE:-$HOME/.local/bin/claude} ]] || echo "  NOTE: no Claude Code at ~/.local/bin/claude (SOSA_CLAUDE=...); boxes need it"
mkdir -p "$HOME_DIR"/{bin,images,stacks,boxes,dl}; chmod 700 "$HOME_DIR"

# --- 1. hypervisor ------------------------------------------------------------
step "cloud-hypervisor $CH_VERSION"
base=https://github.com/cloud-hypervisor
fetch "$base/cloud-hypervisor/releases/download/$CH_VERSION/cloud-hypervisor-static$CH_SUFFIX" "$HOME_DIR/bin/cloud-hypervisor"
fetch "$base/cloud-hypervisor/releases/download/$CH_VERSION/ch-remote-static$CH_SUFFIX" "$HOME_DIR/bin/ch-remote"
fetch "$base/edk2/releases/download/$EDK2_TAG/$FIRMWARE" "$HOME_DIR/bin/$FIRMWARE"
chmod +x "$HOME_DIR/bin/cloud-hypervisor" "$HOME_DIR/bin/ch-remote"
ver=$("$HOME_DIR/bin/cloud-hypervisor" --version); echo "  ${ver%%$'\n'*}"

# --- 2. base image ------------------------------------------------------------
step "Ubuntu 24.04 $ARCH cloud image"
IMG=$HOME_DIR/images/noble-$ARCH.raw
if [[ ! -s $IMG ]]; then
  fetch "$CLOUD_URL/$CLOUD_IMG" "$HOME_DIR/dl/$CLOUD_IMG"
  curl -fsSL -o "$HOME_DIR/dl/SHA256SUMS" "$CLOUD_URL/SHA256SUMS"
  (cd "$HOME_DIR/dl" && grep " \*$CLOUD_IMG\$" SHA256SUMS | sha256sum -c --quiet -) && echo "  checksum ok"
  if command -v qemu-img >/dev/null; then
    qemu-img convert -O raw "$HOME_DIR/dl/$CLOUD_IMG" "$IMG.part"
  elif command -v docker >/dev/null; then
    docker run --rm --platform "linux/$ARCH" -v "$HOME_DIR:/w" alpine sh -c \
      'apk add -q qemu-img >/dev/null && qemu-img convert -O raw /w/dl/'"$CLOUD_IMG"' /w/images/noble-'"$ARCH"'.raw.part \
       && chown '"$(id -u):$(id -g)"' /w/images/noble-'"$ARCH"'.raw.part'
  else
    echo "  need qemu-img or docker to convert the cloud image to raw" >&2; exit 1
  fi
  mv "$IMG.part" "$IMG" && echo "  converted to raw"
else
  echo "  present: $IMG"
fi

# --- 3. the daemon's user unit ------------------------------------------------
step "sosad user unit"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/sosad.service" <<UNIT
[Unit]
Description=sosad: the boxes' daemon (sosa)

[Service]
Environment=SOSA_HOME=$HOME_DIR
ExecStart=/usr/bin/python3 -I $REPO/sosa/daemon.py --home $HOME_DIR
Restart=on-failure
RestartSec=5
TimeoutStopSec=300

[Install]
WantedBy=default.target
UNIT
cat > "$HOME/.config/systemd/user/sosa-front.service" <<UNIT
[Unit]
Description=sosa front door: NAME.ZONE -> the box, woken on request
After=sosad.service
Wants=sosad.service

[Service]
Environment=SOSA_HOME=$HOME_DIR
ExecStart=/usr/bin/python3 -I $REPO/sosa/front.py --home $HOME_DIR
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
echo "  installed ~/.config/systemd/user/sosa-front.service (optional: systemctl --user enable --now sosa-front; config in $HOME_DIR/front.json)"
echo "  installed ~/.config/systemd/user/sosad.service (systemctl --user enable --now sosad; linger: $(loginctl show-user "$USER" -p Linger --value 2>/dev/null))"
echo "  (without the unit, any sosa command starts sosad in the background)"

step "done"
echo "  next: sosa key (the Claude API key the relay holds), sosa stack add $REPO/stacks/hello, sosa stack check hello"
echo "  put $REPO/bin on your PATH, or ln -s $REPO/bin/sosa ~/.local/bin/sosa"
