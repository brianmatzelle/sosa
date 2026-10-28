#!/usr/bin/env bash
# The front door's wake_cmd for hosts on AWS (front.json: {"wake_cmd": "/path/to/sosa/aws/wake.sh"}).
#
#   aws/wake.sh aws:REGION:INSTANCE-ID
#
# Starts the instance (a no-op when it is running) and waits until it is; the front door then waits for the
# daemon. Needs AWS credentials that may ec2:StartInstances and ec2:DescribeInstances: the front door's own
# instance role, or a profile (AWS_PROFILE) where it runs. The handle is what the host announced (its
# ~/.sosa/wake, written by aws/bootstrap.sh).
set -euo pipefail
h=${1:?aws:REGION:INSTANCE-ID}
[[ $h =~ ^aws:([a-z0-9-]+):(i-[0-9a-f]+)$ ]] || { echo "wake.sh: not an aws handle: $h" >&2; exit 2; }
region=${BASH_REMATCH[1]} id=${BASH_REMATCH[2]}
st=$(aws ec2 describe-instances --region "$region" --instance-ids "$id" --query 'Reservations[0].Instances[0].State.Name' --output text)
case $st in
  running) echo "$id is running"; exit 0 ;;
  stopping) aws ec2 wait instance-stopped --region "$region" --instance-ids "$id" ;;
  stopped|pending) ;;
  *) echo "wake.sh: $id is $st" >&2; exit 1 ;;
esac
aws ec2 start-instances --region "$region" --instance-ids "$id" --query 'StartingInstances[0].CurrentState.Name' --output text
aws ec2 wait instance-running --region "$region" --instance-ids "$id"
echo "$id is running"
