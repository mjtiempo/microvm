# microvm-control — design & plan

Status: proposal (pre-implementation)
Scope: a local utility to create, run, clone, back up, restore and delete
Firecracker microVMs on demand, with an LLM-facing command surface in the same
style as `computer-control` and `browser-control-cli`.

## 1. Goal

One verb per process, one JSON object of evidence per call, so an agent can
drive disposable Linux VMs the way it drives a browser or a desktop:

```bash
microvm-control create demo --ttl 1h
microvm-control start demo --wait-ssh
microvm-control exec demo -- chromium --headless --no-sandbox --dump-dom https://example.com
microvm-control clone demo demo2
microvm-control backup demo --out ~/backups
microvm-control restore ~/backups/demo-20261016.tar.zst --id demo3
microvm-control delete demo2 --yes
```

Non-goals: multi-host orchestration, Kubernetes replacement, GPU/USB
passthrough, Windows guests.

## 2. What already exists (assets to reuse)

| Asset | Role in the new design |
| --- | --- |
| `build.sh` | becomes `microvm-control image build` — Alpine base image, headless Chromium, persist drive |
| `net.sh` | becomes `microvm-control net` — tap pool + static NAT (installed once) |
| `run.sh` / `ssh.sh` | replaced by `start` / `ssh` / `exec` (same VM config, API-driven) |
| `test.sh` | proves the rootless userns+netns harness that integration tests will reuse |
| `vm.json` | becomes a per-instance generated config; drives, vcpu, mem, logger |

Verified working today: Alpine 3.24.2 + Chromium 152, FC v1.17.0, kernel
6.18.54, SSH + serial console, `/persist`, snapshots-capable API socket,
zero-root build.

## 3. Host constraints (measured)

- CachyOS, x86_64, KVM via `/dev/kvm` (mode 666), 24 vCPU, 46 GiB RAM.
- `/home` is **btrfs** (930 GiB, 126 GiB free) → `cp --reflink=always` for
  instant COW clones; watch free space, it is at 87 %.
- `python3 3.14.8`, `zstd`, `qemu-img`, `flock`, `jq`, `openssl`, `curl`.
- `sudo` requires a password; user namespaces work.
- Taps are kernel objects: they disappear on host reboot → the pool must be
  re-created by a systemd unit installed once (`net install`).

## 4. Design overview

```text
                    ┌──────────────── microvm-control (CLI, JSON) ───────────────┐
                    │ verbs → store (state) · fcapi (unix socket) · netpool     │
                    │          storage (reflink/debugfs) · sshx · audit         │
                    └───────┬─────────────┬──────────────────┬──────────────────┘
                            │             │                  │
          $MICROVM_HOME/    │      mvtap0..N (pre-created,    │   base images
          instances/<id>/   │      admin-UP, user-owned)      │   images/base/
          meta.json         │             │                  │   (kernel, FC,
          disks/ rootfs     │             │                  │    rootfs, data)
          run/ sock,pid,log │             │                  │
          snapshots/<name>/ │             │                  │
                            └─────────────┴───── NAT (one-time iptables) ───────┘
```

Key decisions:

1. **API-driven lifecycle, not `--config-file`.** Every instance is configured
   through the Unix-socket API (`PUT /boot-source`, `/drives`, `/machine-config`,
   `/network-interfaces`, `/actions`) so snapshot/restore and pre-boot queries
   share one code path.
2. **Zero root at runtime.** `net install` (once, with sudo) creates a pool of
   persistent, admin-UP, user-owned taps and static NAT rules; after that
   `create/start/stop/clone/backup/restore/delete` never need privileges.
3. **btrfs reflink cloning.** Cold clone = reflink of `rootfs.ext4`/`data.ext4`
   (instant, COW). Live clone = pause → reflink disks → full memory snapshot →
   resume.
4. **Identity injection without root.** At create/clone the utility rewrites
   `/etc/hostname`, `/etc/network/interfaces` and `/etc/microvm/identity.env`
   inside the copied ext4 image with `debugfs -w`, and injects fresh SSH host
   keys generated on the host. No guest boot hooks required, deterministic.
5. **Python 3 stdlib only.** `http.client` over `AF_UNIX` for the Firecracker
   API; no dependencies to install; matches the other control CLIs.

## 5. State model

`MICROVM_HOME` (default `${XDG_DATA_HOME:-~/.local/share}/microvm`):

```text
images/base/                     # built by `image build`
  firecracker, jailer, vmlinux -> vmlinux-6.18.54, rootfs.ext4, data.ext4, version.json
instances/<id>/
  meta.json                      # schema, id, state, config, net, keys, timestamps, ttl
  disks/rootfs.ext4, data.ext4   # reflink copies of the base template
  run/firecracker.sock, console.log, firecracker.log, pid
  keys/id_ed25519[.pub]          # control key (see 9.), ssh host keys
  snapshots/<name>/{vmstate,mem,meta.json}
backups/<id>-<ts>.tar.zst        # default output dir, overridable
trash/<id>.<ts>/                 # deleted instances, purged by `gc`
locks/instance-<id>.lock, net.lock, store.lock
net.json                         # tap↔instance allocation, desired pool size
audit.log                        # JSONL, every invocation
```

`meta.json` is the single source of truth:

```json
{
  "schema": 1,
  "id": "demo",
  "state": "running",              // created|starting|running|paused|stopped|dead
  "created_at": "...", "started_at": "...", "expires_at": "...",
  "config": {"vcpu": 2, "mem_mib": 2048, "kernel": "vmlinux-6.18.54",
             "firecracker": "v1.17.0", "boot_args": "console=ttyS0 reboot=k panic=1 root=/dev/vda rw",
             "track_dirty_pages": true},
  "net": {"tap": "mvtap3", "index": 3, "ip": "172.16.12.2", "gw": "172.16.12.1",
          "mac": "06:00:AC:10:0C:02"},
  "disks": {"rootfs": {"path": "disks/rootfs.ext4", "sha256": "..."},
            "data":   {"path": "disks/data.ext4",   "sha256": "..."}},
  "pid": 12345
}
```

On every invocation the store reconciles: `state=running` with a dead pid or
missing socket becomes `dead` (never lie in `status`). Stale sockets are
removed before start.

## 6. Networking: tap pool, no runtime root

`microvm-control net install --taps 8` (sudo, once) and a systemd unit
`microvm-net.service` re-apply after reboot:

- `mvtap<i>`: `ip tuntap add mode tap user $SUDO_USER`, `ip link set up`
  (persistent and admin-UP while no VM is attached).
- Addressing: `/30` per tap — tap `i` gets `172.16.(4i).1`, guest
  `172.16.(4i).2`. Index 0 is reserved for tests/back-compat (`fc-tap`).
- Static NAT, installed once (interface wildcard):

  ```sh
  iptables -t nat -A POSTROUTING -s 172.16.0.0/12 -o "$WAN" -j MASQUERADE
  iptables -A FORWARD -i 'mvtap+' -o "$WAN" -j ACCEPT
  iptables -A FORWARD -i "$WAN" -o 'mvtap+' -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  ```

- Allocation lives in `net.json` under `flock`; `create` fails
  `ERR[no-tap-available]` with the pool size and the fix (`net grow --taps N`)
  when full.
- Test mode: `MICROVM_NET_MODE=userns` makes the CLI create its own tap inside
  a private netns (exactly what `test.sh` does today) → integration tests run
  with **no sudo**.

## 7. Operations semantics

### create

1. validate id (`[a-z0-9][a-z0-9-]{0,30}`), reserve tap + id (locks),
2. reflink base `rootfs.ext4` → `disks/rootfs.ext4`, base `data.ext4` →
   `disks/data.ext4` (or a fresh empty data image with `--fresh-data`),
3. inject identity: hostname, `interfaces` (guest IP/gw), `identity.env`,
   generated SSH host keys; clear `/var/lib/microvm/last-id`,
4. write `meta.json` (ttl, config), state `created`.
   Idempotence: `create demo` on an existing id fails `ERR[already-exists]`
   unless `--reuse` (returns the existing instance unchanged).

### start / stop

- `start`: spawn `firecracker --api-sock …` detached (`setsid`, pid file,
  logs to `run/`), wait for socket, configure via API, `InstanceStart`, then
  wait for SSH readiness (bounded by `--timeout`, default 60 s). Reply carries
  `boot_ms`, `pid`, `ip`, and is `verified` only when the SSH probe succeeded.
- `stop`: `PUT /actions {"action_type":"SendCtrlAltDel"}` (inittab maps it to
  `/sbin/reboot`; the guest's `reboot=k` makes FC exit). If the process is
  still alive after the timeout, SIGTERM, then SIGKILL with an honest
  `effect: "unconfirmed"` note. Always `fsync`-clean compaction is left to ext4
  journal replay; `status` reports `clean` vs `unclean` from the last exit.

### delete

`stop` if needed → optionally `backup` (`--archive`) → move instance dir to
`trash/` (default; reclaimed by `gc`) or remove outright with `--purge`.
Destructive verbs require `--yes` when stdin is not a TTY. Verified by the
path's absence and the tap's release.

### clone

- **Cold** (`clone demo demo2`): reflink disks (instant on btrfs; on
  non-reflink FS falls back to `cp --sparse=always` with a warning), inject a
  new identity. Reply includes `mode: "reflink"` and `shared_extents_bytes`.
- **Live** (`clone demo demo2 --from running`): `PATCH /vm Paused` → reflink
  disks → `PUT /snapshot/create` (full vmstate+mem) → `PATCH /vm Resumed` →
  load the snapshot into a new FC process under the new id. The clone resumes
  from the snapshot, so its memory is the source's; the source is only paused
  for the copy (tens of ms).
- `--fresh-data` starts the clone with an empty `/persist`.
- Version guard: snapshot restore refuses
  (`ERR[snapshot-version-mismatch]`) when Firecracker/kernel/CPU-template of
  the record differs from the running binary.

### backup / restore

- `backup ID [--name X] [--out DIR] [--no-data] [--pause|--cold]`:
  - stopped: archive disks + meta + `manifest.json` (sha256 per member);
  - running: pause → snapshot → reflink staging copy of disks → resume →
    archive; the manifest records the snapshot for point-in-time restore.
  - format: `tar.zst` (zstd -3) by default, `--format raw-dir` for rsync-style
    backups, `--format qcow2` converts disks with `qemu-img` (phase 3).
- `restore ARCHIVE [--id NEW] [--ip auto] [--verify boot]`: unpack into a new
  instance, verify manifest hashes, inject a new identity, register, and with
  `--verify boot` boot it once and run `true` over SSH. Reply lists verified
  hashes. Restores never reuse the archived id unless `--id` says so.

### snapshot (lower-level, for agents that want lineage)

`snapshot create|list|delete`, `snapshot load ID --name X --new-id Y` expose
Firecracker's own snapshot API; `clone --from snapshot` is sugar over it.

### exec / cp / logs

- `exec ID [--timeout S] [--out FILE] [--json] -- CMD...`: run over SSH,
  return `exit_code`, small `stdout`/`stderr` inline, or write large output to
  `--out` and return `path`, `bytes`, `sha256` (the `browser-control` `--out`
  pattern). Never silently truncates: truncation is explicit in the reply.
- `cp SRC DST` with `ID:/path` syntax on either side (`scp`/`sftp` under the
  hood), returns bytes + sha256 for verification.
- `logs ID [--console|--firecracker] [--follow] [--tail N]`: console log is
  the serial output; ideal for post-mortem without console access.

### reap / gc / quotas

- `reap [--older-than 1h]` stops/deletes expired instances (TTL from `meta`),
  runs before every `create` (cheap), and from an optional systemd timer.
- `gc` purges `trash/` older than N days and orphaned snapshot chains.
- Limits in `$MICROVM_HOME/config.json`: `max_instances`, `max_vcpu`,
  `max_mem_mib`, `max_disk_gib`, `default_ttl`. `create` refuses with
  `ERR[quota-*]` and states the current usage. Host free-space check
  (btrfs) refuses below a floor.

## 8. LLM command surface

Adopt the house style: one verb per process; exactly one JSON object on
stdout; `ERR[code]: message` on stderr; exit 0 success, 1 refusal, 2 usage,
130 interrupt; global flags before the verb (`--json` default on, `--quiet`,
`--dry-run`, `--timeout SEC`, `--home DIR`); mutating verbs answer with the
outcome contract:

```json
{
  "schema": 1,
  "ok": true,
  "verified": true,
  "effect": "confirmed",
  "rung": "api+ssh",
  "instance": {"id": "demo", "state": "running", "ip": "172.16.4.2", "ttl_remaining_s": 3500},
  "data": {"boot_ms": 1840, "pid": 4242}
}
```

Refusal:

```json
{"schema": 1, "ok": false,
 "error": {"code": "instance-not-running", "message": "demo is stopped",
           "hint": "microvm-control start demo"}}
```

`effect` is `unverifiable` where no read can prove the act (e.g. `stop` when
the guest vanished before CtrlAltDel landed) — honest, never a fake `ok`.

Verb map (mutating verbs marked ✎):

| group | verbs |
| --- | --- |
| image | `image build` ✎ `image list` `image gc` ✎ |
| host | `doctor` `caps` `audit` |
| net | `net install` ✎ `net uninstall` ✎ `net status` `net grow` ✎ |
| lifecycle | `create` ✎ `start` ✎ `stop` ✎ `restart` ✎ `delete` ✎ `list` `status` `reap` ✎ `gc` ✎ |
| access | `ssh` `exec` `cp` `logs` `console` |
| storage | `snapshot create\|list\|delete` ✎ `clone` ✎ `backup` ✎ `restore` ✎ |

Agent-oriented extras:

- `caps --json`: pool size/free taps, quota usage, btrfs free space, reflink
  support, allowed id pattern — lets the model plan before acting.
- `--dry-run` prints `{"dry_run": true, "plan": [...]}` for mutating verbs.
- `create --ttl` defaults to 30 min when invoked by an agent profile
  (`MICROVM_AGENT=1`) so leaked VMs reap themselves.
- Audit: every invocation appends one JSONL line (verb, args, instance, ok,
  duration, caller env), readable via `audit --tail N`.

Deliverables for the LLM entry point:

- `microvm-control --help` (argparse truth per verb),
- `references/verbs.md`, `references/errors.md` in the repo,
- a skill directory (`skills/microvm-control/SKILL.md`) mirroring the
  `computer-control` skill structure, installed by symlink into
  `~/.pi/agent/skills/`,
- stable `"schema": 1` on every reply; schema bumps are breaking and
  documented.

## 9. Guest identity & security

- Control access uses one image-baked SSH key today (`keys/id_ed25519`).
  Phase 2 adds per-instance keys: `create` generates a keypair, injects the
  public half into the copy, and `delete` removes the private half with the
  instance dir. Until then, note the trade-off in `doctor`.
- Fresh SSH **host** keys per instance (injected at create) so host-key
  pinning works and clones are distinguishable.
- Identical MAC/IP across clones is prevented by the identity injection;
  `clone` refuses to finish if the injected IP equals the source's.
- Firecracker runs with its default seccomp filter; `--enable-pci` is optional
  (guest kernel 6.18 supports it) but kept off until benchmarked.
- Hardened mode (phase 3): `jailer` with a per-instance uid/chroot/cgroup.
  This needs root to launch the jailer, so it is opt-in
  (`create --jail`) and documented as such; the default stays zero-root.
- API socket lives at mode 0600 inside the instance dir; the utility never
  exposes it over TCP.

## 10. Testing

- **Unit** (no KVM): id validation, IP/tap allocation, manifest checksums,
  identity injection round-trip (`debugfs` read-back), lock contention,
  error-code mapping.
- **Integration** (rootless, reused from `test.sh`): build image → create →
  start → exec → cp → clone → stop → backup → restore → verify boot → delete,
  all inside `unshare -Urn` with `MICROVM_NET_MODE=userns`. Runs in CI on any
  KVM host without sudo.
- **Hosted lane** (`net install` done): multi-instance concurrency (3 VMs ×
  2 taps each? — 1 each), pool exhaustion, TTL reap, host reboot reconciliation.
- **Failure injection**: killed FC process, dangling socket, read-only image
  dir, disk full (small tmpfs home), missing tap, mismatched snapshot version,
  corrupt backup (flipped byte → `restore` must refuse).
- Acceptance for every milestone is written as a runnable script under
  `tests/accept/` so "done" is demonstrable, not claimed.

## 11. Migration

`build.sh`, `net.sh`, `run.sh`, `ssh.sh` remain as thin wrappers
(`exec microvm-control …`) so existing docs keep working; they are deleted in
M2 once the CLI covers them and `README.md` points at the CLI.

## 12. Milestones

| # | Deliverable | Acceptance (runnable) | Size |
| --- | --- | --- | --- |
| ✅ M0 | scaffolding: `fcapi`, `store`, `errors`, JSON envelope; full lifecycle on the existing `images/` + `fc-tap` (`create/start/stop/restart/status/list/exec/cp/logs/delete/caps/doctor/image build`) | `tests/accept/m0.sh` green rootless: create → start → exec → cp → status → logs → stop → delete | S–M |
| M1 | tap pool + `net install` systemd unit + `create/delete/exec/cp/logs/reap` + locks + `doctor` + userns test lane | 3 instances concurrently, each own tap/IP; create→exec→delete < 10 s; `tests/accept/m1.sh` green rootless | M |
| M2 | snapshot/clone: reflink cold clone, live clone, identity injection, snapshot guard | `clone demo demo2` < 2 s, demo2 boots with new host keys/IP while demo keeps running; source resumes cleanly | M |
| M3 | backup/restore: manifest + sha256 + tar.zst, running backup, restore with `--verify boot` | backup of running instance restored on a new id; pre-backup file present; flipped-byte archive refused | M |
| M4 | LLM entry: references, skill, `caps`, `--dry-run`, agent TTL, audit | pi agent completes a full lifecycle from the skill doc alone; every reply carries `ok/verified/effect` | S–M |
| M5 | hardening & polish: jailer opt-in, per-instance keys, vsock control channel, qcow2 export, snapshot-lineage GC, `browser` helper verb (guest Chromium screenshot/pdf/dom as a first-class verb) | `create --jail` boots confined; per-instance keys verified; GC reclaims truncated snapshot chains | L |

Sizes: S ≈ half a day, M ≈ 1–2 days, L ≈ 3+ days, at this repo's current
pace.

Status: **M0 delivered** (2026-10-16) and passing `tests/accept/m0.sh`;
JSON contract with `ok/verified/effect/rung`, `ERR[code]`
refusals, `--dry-run`, per-instance identity (hostname, `/30`-ready interfaces,
fresh ssh host keys) injected with `debugfs`, and reflinked disks. Next: M1
(`netpool.py`, tap pool + `net install` systemd unit, `reap`, per-instance ssh
keys).

## 13. First actions (next session)

1. Create the package skeleton and the error/envelope contract
   (`errors.py`, `store.py`) — the contract is what everything else returns.
2. Implement `fcapi.py` (Unix-socket HTTP) against the existing images and
   validate with a hello-world `start/status/stop` on `fc-tap`.
3. Port `net.sh` to `netpool.py` with a `net status` reply and the systemd
   unit generator; keep the userns test mode.
4. Write `tests/accept/m0.sh` (rootless) and wire it into `test.sh`.
5. Only then expand verbs (M1).

## 14. Decisions (confirmed)

| # | Question | Decision |
| --- | --- | --- |
| 1 | State dir | `~/.local/share/microvm`, `MICROVM_HOME` override |
| 2 | Language | Python 3 stdlib only |
| 3 | One-time `sudo net install` | accepted |
| 4 | SSH keys | per-instance in M2; single control key until then |
| 5 | Live clone | full memory snapshot in M2, diff chains later |
| 6 | Daemon | CLI-only; daemon only if concurrent agent access demands it |
| 7 | Tap pool | 8 taps, `/30` per tap, index 0 reserved |
| 8 | Name | `microvm-control` |

M0 deviation: instance `meta.json` records `size_bytes` per disk; sha256 hashes
arrive with backup/snapshot manifests (M3), not on every create.

## 15. Risks

- **Disk space** (btrfs at 87 %): reflink clones are cheap, but backups and
  snapshots are not; quotas and the free-space floor in `caps` are mandatory,
  plus `gc`/`reap` defaults that clean up.
- **Snapshot fragility**: tied to Firecracker version/CPU template; the
  version guard turns a confusing boot failure into a clear refusal.
- **Firecracker has no ACPI power button**: `stop` depends on CtrlAltDel/guest
  cooperation; hard kills are modeled as `unclean` in `status`, never hidden.
- **Identity injection is image-format-specific** (ext4 + `debugfs`); the
  `doctor` verb checks for `debugfs` and the base image format, and create
  refuses with a clear code otherwise.
