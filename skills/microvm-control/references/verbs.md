# microvm-control verbs

Top-level verbs: `image` `net` `clone` `snapshot` `reap` `gc` `backup`
`restore` `create` `start` `stop` `restart` `status` `list` `exec` `cp`
`ssh` `logs` `delete` `browser` `caps` `doctor` `audit`.

Every non-interactive invocation prints exactly one JSON object on stdout.
Global flags come before the verb: `--home DIR`, `--quiet`, `--dry-run`,
`--timeout SEC`, `--version`.

Reply envelope:

```json
{"schema": 1, "ok": true, "verified": true, "effect": "confirmed",
 "rung": "api+ssh", "instance": {...}, "data": {...}}
```

Refusal:

```json
{"schema": 1, "ok": false,
 "error": {"code": "not-running", "message": "...", "hint": "..."}}
```

## Image and host

| verb | flags | data |
| --- | --- | --- |
| `image build` | `--force` | `base_dir`, `firecracker`, `kernel`, `alpine`, sizes |
| `doctor` | — | `checks[]` (`name`, `ok`, `required`, `detail`); exit 1 when a required check fails |
| `caps` | — | base versions, instances, limits, disk, host tools, net mode |
| `audit` | `--tail N` | `entries[]` JSONL of recent invocations, `path` |

## Network pool (root verbs need sudo)

| verb | flags | notes |
| --- | --- | --- |
| `net init` | `--taps N` | define the pool in state (no root) |
| `net status` | — | pool, per-tap state (`exists`, `up`, `allocated_to`), `free`, service, NAT |
| `net install` | `--taps N`, `--user NAME` | write `net-setup.sh` + `microvm-net.service`, enable + start |
| `net grow` | `--taps N` | resize (refuses `pool-shrink` while higher taps are allocated) |
| `net uninstall` | — | stop + remove unit, script and pool state |

## Instances

| verb | flags | data / notes |
| --- | --- | --- |
| `create ID` | `--vcpu N` `--mem MIB` `--ttl SEC` `--tap` `--ip` `--gw` `--prefix` `--fresh-data` | reflinked disks + fresh identity, verified by read-back; auto-allocates a pool tap |
| `clone SRC ID` | `--fresh-data` `--vcpu` `--mem` `--ttl` `--tap` `--ip` `--gw` `--prefix` | running source is paused for the reflink copy and resumed; copy boots cold |
| `start ID` | `--no-wait-ssh` `--timeout SEC` | verified by SSH probe; `already_running: true` is a valid reply |
| `stop ID` | `--timeout SEC` | CtrlAltDel; `graceful: false` + `effect: unconfirmed` if signals were needed |
| `restart ID` | `--no-wait-ssh` `--timeout SEC` | stop then start |
| `status ID` | `--probe` | `state`, `running`, ttl; `--probe` adds `ssh_reachable` |
| `list` | — | `instances[]` summaries; reconciles dead pids |
| `exec ID` | `--out FILE` `--timeout SEC`, then `-- CMD ARGS...` | `exit_code`, `stdout`, `stdout_bytes`, `stdout_sha256`, `out_path`, `duration_ms` |
| `cp SRC DST` | — | one side is `ID:/guest/path`, the other local |
| `ssh ID` | — | interactive shell (replaces the process) |
| `logs ID` | `--firecracker` `--tail N` | `lines[]`, `path` |
| `delete ID` | `--yes` `--purge` `--force` `--timeout SEC` | moves to `<home>/trash` unless `--purge` |
| `browser ID` | `--dom URL` \| `--screenshot URL` \| `--pdf URL`, `--out FILE`, `--window-size W,H`, `--timeout SEC` | headless Chromium in the guest: DOM text, or a verified PNG/PDF (`out_path`, `bytes`, `sha256`, `magic_ok`) |
| `reap` | `--older-than SEC` `--timeout SEC` | deletes TTL-expired instances (and, with `--older-than`, long-stopped ones) |
| `gc` | `--trash-days N` `--artifact-days N` `--snapshot-days N` | removes old trash entries, exec artifacts and snapshots (`--snapshot-days` is opt-in) |

## Snapshots

| verb | flags | data |
| --- | --- | --- |
| `snapshot create ID` | `--name NAME` | pauses, creates a full snapshot, resumes; `snapshot.vmstate_bytes`, `mem_bytes` |
| `snapshot list ID` | — | `snapshots[]`, `count` |
| `snapshot delete ID` | `--name NAME` | `freed_bytes` |
| `snapshot load ID` | `--name NAME` `--force` `--timeout SEC` | stops the instance, loads the memory state, waits for SSH; refuses `snapshot-version-mismatch`, and `disk-modified` when a disk changed size (`--force` overrides) |

## Backup and restore

| verb | flags | data |
| --- | --- | --- |
| `backup ID` | `--out DIR` `--name NAME` `--no-data` `--no-pause` `--format tar.zst\|raw-dir\|qcow2` | `archive`, `bytes`, `sha256`, `files[]` (name/size/sha256), `paused_source`; the manifest is read back from the archive |
| `restore ARCHIVE` | `--id NEW` `--verify` `--fresh-data` `--ttl SEC` `--tap` `--ip` `--gw` `--prefix` `--timeout SEC` | `files_verified`, `disk_format`, `boot_verified`; every sha256 is verified before the instance is created |

## Exit statuses

| code | meaning |
| --- | --- |
| 0 | ok (including `doctor` all-green) |
| 1 | refusal (`ERR[code]` on stderr, `ok: false` on stdout) |
| 2 | usage error (argparse, on stderr) |
| 130 | interrupted |
