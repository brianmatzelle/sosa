#!/usr/bin/env bash
# On a host built to be the host image (aws/host.yaml with Mode=bake; aws/sosa-aws image runs the whole thing), as
# root, after aws/bootstrap.sh: bake every stack's image inside it (sosa image STACK, in a box each), then take out
# everything that makes this host this host (its token, secrets, key, boxes, logs, machine id, the instance's
# cloud-init state) and power off, so aws/sosa-aws can snapshot it as an AMI. A host from that AMI comes up with
# everything installed (/etc/sosa/image says so) and only does what's its own.
set -Eeuo pipefail
# shellcheck source=/dev/null
. /etc/sosa/aws.env
U=sosa H=/home/sosa
REPO=$H/sosa
UID_=$(id -u $U)
# (not sudo -i: it backslash-escapes the command, and a glob or a ~ in it expands to nothing)
as_u() { sudo -u $U -H env XDG_RUNTIME_DIR=/run/user/$UID_ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_/bus bash -lc "$*"; }
status() { aws ssm put-parameter --region "$REGION" --name "/sosa/$STACK/status" --type String --overwrite --value "$1" >/dev/null 2>&1 || true; }
step() { printf '\n== %s\n' "$*"; status "baking: $*"; }
trap 'status "bake failed at line $LINENO (see /var/log/sosa-bootstrap.log)"' ERR

for d in "$H"/.sosa/stacks/*/; do
  [[ -f $d/stack.toml ]] || continue
  s=$(basename "$d")
  step "the image for stack $s"
  as_u "sosa image $s" | tail -3 | sed 's/^/   /'
done
as_u "sosa images" | sed 's/^/   /'

step "this host out of it"
as_u "systemctl --user stop sosad" || true
systemctl stop sosa-host-idle.timer 2>/dev/null || true
rm -rf "$H"/.sosa/boxes/* "$H"/.sosa/dl
rm -f "$H"/.sosa/{api-token,join-secret,join.json,anthropic-key,workspace,wake,usage.jsonl,sosad.log,sosad.out,sosad.pid,sosad.sock,sosad.lock,front.log,front.out,front-hosts.json,front-register-secret}
rm -rf "$H"/.bash_history "$H"/.claude/projects "$H"/.claude/history.jsonl "$H"/.claude/sessions 2>/dev/null || true
rm -rf /root/.bash_history /var/lib/sosa
rm -f /etc/sosa/aws.env
echo "sosa host image: repo $(git -c safe.directory='*' -C "$REPO" rev-parse --short HEAD), $(date -u +%FT%TZ)" > /etc/sosa/image
as_u "sosa images" >> /etc/sosa/image
status "baked: powering off for the snapshot"
apt-get clean
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -f /var/log/sosa-bootstrap.log
cloud-init clean --logs --seed --machine-id >/dev/null 2>&1 || { cloud-init clean --logs --seed; truncate -s 0 /etc/machine-id; }
rm -f /etc/ssh/ssh_host_*
sync
systemd-run --quiet --on-active=5 systemctl poweroff
echo "baked; powering off"
