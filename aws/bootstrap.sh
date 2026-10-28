#!/usr/bin/env bash
# Everything on a sosa host after the clone, as root. aws/host.yaml's user-data runs it at first boot; it is
# idempotent, so re-run it over SSM after changing it: aws/sosa-aws run HOST -- sudo aws/bootstrap.sh
#
# The host (an EC2 instance with nested virtualization) is laid out like a workstation running sosa: the `sosa`
# user owns the repo at ~/sosa and the state in ~/.sosa, runs sosad as a user unit, and the boxes inside it.
#   sosad (user unit)        the daemon, on 0.0.0.0:8790 for the front door (the security group decides who)
#   sosa-host-idle (timer)   aws/host-idle.py: the host stops itself when no box has run for IDLE_MINUTES
# What's a host's own comes from SSM at boot: the Claude API key every host may use (/sosa/shared/anthropic-key),
# the front door's register secret (/sosa/shared/front-register-secret), the key's workspace. Stacks come from
# s3://BUCKET/stacks/ (aws/sosa-aws stacks DIR...).
set -Eeuo pipefail
# shellcheck source=/dev/null
. /etc/sosa/aws.env
U=sosa
H=/home/$U
REPO=$H/sosa
UID_=$(id -u $U)

step() { printf '\n== %s\n' "$*"; }
status() {
  aws ssm put-parameter --region "$REGION" --name "/sosa/$STACK/status" --type String \
    --overwrite --value "$1" >/dev/null 2>&1 || true
}
trap 'status "bootstrap failed at line $LINENO (see /var/log/sosa-bootstrap.log)"' ERR
# as the sosa user, with its user manager reachable (for systemctl --user)
as_u() { sudo -iu $U env XDG_RUNTIME_DIR=/run/user/$UID_ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_/bus bash -c "$*"; }
param() { aws ssm get-parameter --region "$REGION" --name "$1" --with-decryption --query Parameter.Value --output text 2>/dev/null || true; }
imds() {  # IMDSv2
  local tok
  tok=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -s -m 2 -H "X-aws-ec2-metadata-token: $tok" "http://169.254.169.254/latest/meta-data/$1"
}

status "bootstrapping"
step kvm
[[ -c /dev/kvm ]] || modprobe kvm_intel 2>/dev/null || true
if [[ ! -c /dev/kvm ]]; then
  apt-get install -y -q "linux-modules-extra-$(uname -r)" >/dev/null 2>&1 || true
  modprobe kvm_intel 2>/dev/null || true
fi
[[ -c /dev/kvm ]] || { status "no /dev/kvm: nested virtualization is off for this instance type"; exit 1; }
# the boxes are reached over vsock: the device belongs to the kvm group, loaded at boot
echo vhost_vsock > /etc/modules-load.d/sosa.conf
modprobe vhost_vsock || true
echo 'KERNEL=="vhost-vsock", GROUP="kvm", MODE="0660"' > /etc/udev/rules.d/60-sosa-vsock.rules
udevadm control --reload && udevadm trigger --name-match=vhost-vsock || true

step packages
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q xorriso e2fsprogs util-linux qemu-utils python3 jq curl git unzip rsync >/dev/null
usermod -aG kvm $U

step swap
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile
fi
swapon --show | grep -q /swapfile || swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

step "user manager for $U"
loginctl enable-linger $U
for _ in $(seq 1 30); do [[ -S /run/user/$UID_/bus ]] && break; sleep 1; done
# the clone (sudo -iu) started the user manager before the kvm group: its units need a restart to get it
mgr=$(systemctl show -p MainPID --value "user@$UID_" 2>/dev/null || echo 0)
kgid=$(getent group kvm | cut -d: -f3)
if [[ ${mgr:-0} != 0 ]] && ! grep '^Groups:' "/proc/$mgr/status" | grep -qw "$kgid"; then
  systemctl restart "user@$UID_"
  for _ in $(seq 1 30); do [[ -S /run/user/$UID_/bus ]] && break; sleep 1; done
fi
ln -sf "$REPO/bin/sosa" /usr/local/bin/sosa

step "sosa install (hypervisor, base image, the daemon's unit)"
as_u "cd $REPO && ./install.sh" | sed 's/^/   /'

step "Claude Code for the boxes"
as_u 'command -v ~/.local/bin/claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash' | tail -1

step "what's this host's own"
install -d -m 700 -o $U -g $U "$H/.sosa"
# the daemon's TCP address, for the front door; the token is made on the daemon's first start
echo "0.0.0.0:8790" > "$H/.sosa/tcp"
# the wake handle: what the front door runs aws/wake.sh with when this host is stopped
echo "aws:$REGION:$(imds instance-id)" > "$H/.sosa/wake"
key=$(param /sosa/shared/anthropic-key)
if [[ -n $key && $key != None ]]; then
  (umask 077; printf '%s\n' "$key" > "$H/.sosa/anthropic-key"); echo "   Claude API key: from /sosa/shared/anthropic-key"
else
  echo "   no /sosa/shared/anthropic-key: boxes will need tokens (aws/sosa-aws key stores one for every host)"
fi
ws=$(param /sosa/shared/workspace)
if [[ -n $ws && $ws != None ]]; then printf '%s\n' "$ws" > "$H/.sosa/workspace"; fi
unset key ws
if [[ -n ${FRONT_URL:-} ]]; then
  sec=$(param /sosa/shared/front-register-secret)
  if [[ -n $sec && $sec != None ]]; then
    (umask 077; printf '%s\n' "$sec" > "$H/.sosa/join-secret")
    jq -cn --arg u "$FRONT_URL" --arg n "${HOST_NAME:-$STACK}" --arg a "http://$(imds local-ipv4):8790" \
      '{url: $u, name: $n, advertise: $a}' > "$H/.sosa/join.json"
    echo "   joins the front door $FRONT_URL as ${HOST_NAME:-$STACK}"
  else
    echo "   FrontUrl is set but there is no /sosa/shared/front-register-secret (aws/sosa-aws front URL stores it)"
  fi
  unset sec
fi
chown -R $U:$U "$H/.sosa"
chmod 600 "$H/.sosa"/{anthropic-key,join-secret} 2>/dev/null || true

step "stacks from s3://$BUCKET/stacks/"
install -d -o $U -g $U "$H/stacks"
as_u "aws s3 sync --quiet --delete s3://$BUCKET/stacks/ ~/stacks/ && ls ~/stacks" || echo "   (none, or no access)"
as_u 'for d in ~/stacks/*/; do [ -f "$d/stack.toml" ] && sosa stack add "$d" | head -1; done; true'

step "the daemon"
as_u "systemctl --user enable -q sosad && systemctl --user restart sosad"
for _ in $(seq 1 30); do [[ -S $H/.sosa/sosad.sock ]] && break; sleep 1; done
as_u "sosa --json stack list >/dev/null && sosa stack list"

step "host idle stop (aws/host-idle.py, every minute; IDLE_MINUTES=$IDLE_MINUTES)"
install -d -m 755 /var/lib/sosa
cat > /etc/systemd/system/sosa-host-idle.service <<EOF
[Unit]
Description=sosa host: stop the instance once no box has run for a while

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I $REPO/aws/host-idle.py
EOF
cat > /etc/systemd/system/sosa-host-idle.timer <<'EOF'
[Unit]
Description=sosa host idle check, every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable -q --now sosa-host-idle.timer

status "${READY_STATUS:-ready}"
step "${READY_STATUS:-ready}"
