# sosa on AWS

A host is an EC2 instance with nested virtualization running sosad and its boxes. Many hosts stand behind one
front door; a backend talks to the front door alone. Hosts stop themselves when idle and are started again by
the front door on the next request, so a fleet of hosts costs what people actually use.

```
browser / backend ──► front door (sosa front, anywhere that reaches the VPC; your login in front of it)
                        ├─ host-1  EC2, nested KVM: sosad on :8790, boxes inside, stops itself when idle
                        ├─ host-2
                        └─ ...     each announces itself (sosa join) and its wake handle (aws:REGION:i-...)
```

| File | What |
|---|---|
| `base.yaml` | once per account and region: the bucket hosts pull stacks from, the hosts' security group (daemon port from the front door only; no ssh, ops over SSM) |
| `host.yaml` | one host: role scoped to its own parameters, launch template with nested virtualization and IMDSv2, the instance, a first-boot user-data that clones this repo and runs `bootstrap.sh` |
| `bootstrap.sh` | on the host, as root, idempotent: kvm and vsock, packages, the `sosa` user with linger, `install.sh`, Claude Code, the key and the front door's secret from SSM, stacks from S3, the daemon on `0.0.0.0:8790`, the idle timer |
| `bake.sh` | on a `Mode=bake` host: every stack's image, then the host's identity stripped and a power-off for the AMI snapshot |
| `host-idle.py` | the timer's script: once no box has run for `IdleMinutes` and nobody is in an SSM session, `poweroff` (which stops the instance) |
| `wake.sh` | the front door's `wake_cmd`: `aws:REGION:INSTANCE` -> `ec2 start-instances` and wait |
| `sosa-aws` | the CLI from your machine: `base`, `key`, `front`, `stacks`, `image`, `up`, `status`, `run`, `shell`, `stop`, `start`, `down` |

## Setting up

```sh
export AWS_PROFILE=work AWS_REGION=us-east-1
aws/sosa-aws base                      # the bucket and the security group
aws/sosa-aws key                       # the Claude API key hosts' relays hold (typed hidden -> SSM SecureString)
aws/sosa-aws stacks ~/stacks/java-react  # your stacks, to S3
aws/sosa-aws front http://10.0.0.9:8180  # the front door's URL and its register secret (hidden -> SSM)
aws/sosa-aws image                     # the host image: a builder bakes every stack, ~30-60 min, then it's an AMI
aws/sosa-aws up host-1 --idle 30       # a host from the image; ready in a few minutes, registered with the front door
```

Secrets never travel on argv or in chat: `key` and `front` read with hidden input and store SecureStrings; a
host reads them at boot through its role. Only `/sosa/shared/*` is readable by every host; `/sosa/HOST/*` by its
own.

The front door runs wherever your backend is (an instance in the VPC, or your hub if it reaches the hosts'
subnet), with `aws/wake.sh` as its `wake_cmd` and credentials that may start instances (its instance role, or a
profile). The security group lets the daemon port in from the CIDR or security group you give `base`.

## A host's life

A host from the image boots, runs `bootstrap.sh` (which only does what's its own: the token, the key, the
join, the stacks that changed), registers, and is ready. Boxes are created through the front door
(`POST /_sosa/api/v1/boxes`, which picks the emptiest host with the stack's image) or on the host
(`aws/sosa-aws run host-1 -- sosa create NAME --stack S --idle 30 --start`). Boxes with an idle timer sleep on
their own; once every box on a host is asleep for `IdleMinutes`, the host stops. A request for any of its boxes
starts it again: the front door runs `wake.sh`, waits for the daemon, wakes the box, and serves. From a stopped
instance that is about a minute.

A stack's image is baked into the host image, so a new box from it starts in seconds. A new stack, or a changed
one: `aws/sosa-aws stacks DIR` then `aws/sosa-aws image` for the next hosts, and on a running host
`aws/sosa-aws run host-1 -- 'sudo aws/bootstrap.sh && sosa image STACK'`.

## Moving a box between hosts

Not automated yet. A box is its directory under `~/.sosa/boxes/` (disk, config, prompt, transcripts); the bucket
has a `transfers/HOST/` prefix each host may write and read for exactly that.
