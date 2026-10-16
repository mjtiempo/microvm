---
name: microvm-control
description: Drive Firecracker microVMs on this machine through microvm-control — create, start, exec, copy files, clone, snapshot, back up, restore and delete disposable Linux VMs, and manage the host tap pool. Use for isolated execution, throwaway Linux environments, browser/headless work, or any task that needs its own VM rather than this machine.
---

# microvm-control — the microVM as a command surface

One VM action is one `microvm-control <verb>` process, run on the machine that
hosts the VMs. `microvm-control` is on `PATH` (a symlink to the repo launcher).

- **stdout**: exactly one JSON object for every non-interactive verb.
- **stderr**: `ERR[code]: message` on a refusal (plus a `hint:` line when known).
- Exit status: `0` ok, `1` refusal, `2` usage error, `130` interrupt.
- Every reply carries `"schema": 1`. Mutating verbs also carry the outcome
  contract: `ok`, `verified`, `effect` (`confirmed`|`unconfirmed`|`unverifiable`)
  and `rung` (how it was done).
- Global flags go **before** the verb: `--home DIR`, `--quiet`, `--dry-run`,
  `--timeout SEC`. `--version` prints the version.

## First call: prove the install

```bash
microvm-control doctor    # host checks; exit 1 if a required check fails
microvm-control caps      # base image versions, pool, limits, free disk
microvm-control list      # existing instances (may be empty)
```

If `doctor` reports `base-image` missing, run `microvm-control image build`
(one time; imports the prepared Alpine + headless Chromium image set).

## Stance: verify, or refuse

A mutation is never `ok: true` on a bare acknowledgement:

| verb | verified by |
| --- | --- |
| `create` / `clone` / `restore` | reading the new identity back from the disk image |
| `start` / `snapshot load` | an SSH probe into the guest (`ssh_ready_ms`); with `--no-wait-ssh`, `start` says `verified: false` / `effect: "unverifiable"` |
| `stop` | the Firecracker process being gone; `unconfirmed` if signals were needed |
| `snapshot create` | snapshot files exist and are non-empty |
| `backup` | the manifest is read back out of the archive |
| `restore` | every manifest sha256 verified before the instance is created |
| `delete` | the instance directory is gone |
| `exec` | the command's own exit code (`exit_code` is data, not a refusal) |

`effect: unverifiable` is honest, not a failure. A read-back that does not see
the effect is a refusal, not `ok`.

## Canonical lifecycle

```bash
# create → run → use → destroy
microvm-control create demo --ttl 3600
microvm-control start demo | jq '{verified, ssh_ms: .data.ssh_ready_ms}'
microvm-control exec demo -- uname -a
microvm-control exec demo -- chromium --headless --no-sandbox --dump-dom https://example.com
microvm-control browser demo --screenshot https://example.com --out ./shot.png
microvm-control cp demo:/persist/work/shot.png ./shot.png
microvm-control stop demo
microvm-control delete demo --yes          # moved to <home>/trash
```

`start` waits for SSH by default (`--no-wait-ssh` to skip). `stop` uses the
Firecracker API (CtrlAltDel); a guest `reboot` also exits the VM. Destructive
verbs need `--yes` when stdin is not a TTY, and `--dry-run` prints the plan
without touching anything.

## Networking

Instances get their own tap and `/30` address from a host pool:

```bash
sudo microvm-control net install --taps 8   # once, plus a systemd unit
microvm-control net status | jq .data       # taps, allocations, service, NAT
```

`create` allocates a free tap automatically; `delete` releases it; several VMs
run concurrently. Without a pool, `create` falls back to the shared `fc-tap`
and a second concurrent start refuses `net-conflict`. If the pool is exhausted,
the refusal is `no-tap-available` with the `net grow` fix. If a start reports
`tap-missing`/`tap-down`, the pool needs `sudo microvm-control net install`
(or `sudo ./net.sh up` for the legacy tap).

## Cloning, snapshots, backup

```bash
microvm-control clone demo demo2                  # running source: pause, copy, resume
microvm-control clone demo demo2 --fresh-data     # empty /persist
microvm-control snapshot create demo --name s1    # memory + device state
microvm-control snapshot load demo --name s1      # restore that memory state
microvm-control backup demo --name nightly        # tar.zst + sha256 manifest
microvm-control restore <home>/backups/demo-nightly.tar.zst --id demo3 --verify
```

`clone` is disk-level and always gives the copy a fresh hostname, SSH key and
host keys; the copy boots cold. `snapshot` preserves the *memory* of the same
instance (the disks are not reverted). `backup` archives disks + meta + a
sha256 manifest; `restore` verifies every hash before materialising a new
instance, and `--verify` boots it and waits for SSH.

## Addressing and replies

- Instance ids and snapshot names: `[a-z0-9][a-z0-9-]{0,30}`.
- `exec ID -- CMD ARGS...`: large stdout is written to a file and the reply
  gives `out_path`, `stdout_bytes`, `stdout_sha256`; it is never silently
  truncated.
- `error.detail` carries structured evidence (list of failed hashes, API fault
  message, tap list, …). Branch on `error.code`, not on message text.
- `audit --tail N` returns the JSONL trail of every invocation
  (`ts`, `verb`, `instance`, `argv`, `ok`, `code`, `duration_ms`).

## When to use this

- A task needs a disposable, isolated Linux box with root, a persistent
  `/persist`, and headless Chromium.
- Testing something that could damage the host or needs a clean environment.
- Parallel experiments: one VM per experiment, cloned or restored from a
  prepared snapshot/backup.
- Not for: running the user's desktop apps (see `computer-control`) or driving
  the user's own browser (see `browser-control`/pi-chrome).

References: [`references/verbs.md`](references/verbs.md) for every verb and
flag, [`references/errors.md`](references/errors.md) for every refusal code.
