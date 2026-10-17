# microvm-control verbs

Top-level verbs: `image` `net` `clone` `snapshot` `reap` `gc` `backup`
`restore` `create` `start` `stop` `restart` `status` `list` `exec` `cp`
`ssh` `logs` `delete` `browser` `caps` `doctor` `audit`.

Every invocation that parses prints exactly one JSON object on stdout; a usage
error (exit 2) prints argparse usage on stderr and no JSON.
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
| `start ID` | `--no-wait-ssh` `--timeout SEC` | verified by SSH probe; `already_running: true` is a valid reply. With `--no-wait-ssh` the reply is honest: `verified: false`, `effect: "unverifiable"` |
| `stop ID` | `--timeout SEC` | CtrlAltDel; `graceful: false` + `effect: unconfirmed` if signals were needed |
| `restart ID` | `--no-wait-ssh` `--timeout SEC` | stop then start |
| `status ID` | `--probe` | `state`, `running`, ttl; `--probe` adds `ssh_reachable` |
| `list` | — | `instances[]` summaries; reconciles dead pids |
| `exec ID` | `--out FILE` `--timeout SEC`, then `-- CMD ARGS...` | `exit_code`, `stdout`, `stdout_bytes`, `stdout_sha256`, `stdout_truncated`, `out_path`, `stderr`, `stderr_truncated`, `duration_ms`; stdout over 16 KiB spills to `out_path` |
| `cp SRC DST` | — | one side is `ID:/guest/path`, the other local |
| `ssh ID` | — | interactive shell (replaces the process) |
| `logs ID` | `--firecracker` `--tail N` | `lines[]`, `path` |
| `delete ID` | `--yes` `--purge` `--force` `--timeout SEC` | moves to `<home>/trash` unless `--purge` |
| `browser ID` | `--dom URL` \| `--screenshot URL` \| `--pdf URL`, `--out FILE`, `--window-size W,H`, `--timeout SEC` | one-shot legacy form; see **Browser** below |
| `reap` | `--older-than SEC` `--timeout SEC` | deletes TTL-expired instances (and, with `--older-than`, long-stopped ones) |
| `gc` | `--trash-days N` `--artifact-days N` `--snapshot-days N` | removes old trash entries, exec artifacts and snapshots (`--snapshot-days` is opt-in) |

## Browser

`browser ID <action>` drives a long-running headless Chromium inside the
guest over CDP; the legacy `--dom`/`--screenshot`/`--pdf` flags stay one-shot
and unchanged. Nothing auto-starts: a stopped VM refuses `not-running`, a
stopped browser refuses `browser-not-running` with a `start` hint. The
browser's profile is the image's `/persist/chromium` (shared logins), the
launch reads the RESOLVED port out of `DevToolsActivePort`, carries a windowed
user-agent built from the guest binary's own version plus
`--disable-blink-features=AutomationControlled`, and every call
re-verifies the recorded guest pid and the endpoint fingerprint before
driving anything. Mutations are verified by read-back (`changed`, `moved`,
`checked`, `length_after`, the PNG's own IHDR); `js`/`press`/`--at` say
`verified: false` honestly.

| command | flags | data |
| --- | --- | --- |
| `browser ID start` | `--port N` (default 9222; 0 = dynamic), `--timeout SEC` | `pid`, `port` (resolved), `port_requested`, `profile`, `fingerprint`, `already_running` (`adopted: true` when a live orphan was re-recorded instead of launched) |
| `browser ID stop` | `--timeout SEC` | `pid`, `graceful`, `stopped` |
| `browser ID restart` | `--port N`, `--timeout SEC` | as `start` |
| `browser ID status` | `--timeout SEC` | `running`, `pid`, `port`, `uptime_s`, `tabs`, `fingerprint`, `code`/`reason` when unverified |
| `browser ID open URL…` | `--timeout SEC` | `opened[]`, `count` |
| `browser ID tabs` | `--timeout SEC` | `tabs[]` (`id`, `title`, `url`, `active`), `count` |
| `browser ID close SPEC…` | `--all`, `--timeout SEC` | `closed[]`, `count` |
| `browser ID nav URL` | `--tab SPEC`, `--timeout SEC` | `url_read`, `moved`, `ready` |
| `browser ID back` | `--tab SPEC`, `--timeout SEC` | `moved`, `before`/`after` |
| `browser ID forward` | `--tab SPEC`, `--timeout SEC` | `moved`, `before`/`after` |
| `browser ID reload` | `--tab SPEC`, `--timeout SEC` | `before`/`after` (`performance.timeOrigin`) |
| `browser ID text` | `--tab SPEC`, `--selector CSS`, `--chars N`, `--timeout SEC` | `text`, `length`, `truncated` |
| `browser ID find [TEXT]` | `--tab SPEC`, `--selector CSS`, `--cap N`, `--timeout SEC` | `matches[]` (`box`, `center`, `hit`), `count`, `total` |
| `browser ID extract` | `--each CSS`, `--field NAME=SPEC` (repeatable), `--cap N`, `--chars N`, `--visible`, `--unique FIELD`, `--tab SPEC`, `--timeout SEC` | `matches[]` records, `count`, `total`, `truncated` |
| `browser ID dom [URL]` | `--out FILE`, `--tab SPEC`, `--timeout SEC` | `dom`, or `out_path` + `dom_sha256` when large |
| `browser ID click [TEXT]` | `--selector CSS`, `--index N`, `--at X,Y`, `--tab SPEC`, `--timeout SEC` | `clicked`, `changed`, `before`/`after` (url/title/scroll); `--at` adds `under` and `verified: false` |
| `browser ID hover [TEXT]` | `--selector CSS`, `--index N`, `--tab SPEC`, `--timeout SEC` | `hovered` |
| `browser ID focus [TEXT]` | `--selector CSS`, `--index N`, `--tab SPEC`, `--timeout SEC` | `focused` |
| `browser ID press KEY` | `--tab SPEC`, `--timeout SEC` | `dispatched` (always `verified: false`) |
| `browser ID insert TEXT` | `--tab SPEC`, `--timeout SEC` | `length_before`/`length_after`, `verified` |
| `browser ID type TEXT` | `--delay SEC` (0–5 between keystrokes), `--tab SPEC`, `--timeout SEC` | `length_before`/`length_after`, `delay_s`, `verified` |
| `browser ID check [TEXT]` | `--selector CSS`, `--index N`, `--uncheck`, `--tab SPEC`, `--timeout SEC` | `checked`, `checked_before`, `changed` |
| `browser ID select [TEXT]` | `--selector CSS` (required), `--index N`, `--value V` (required), `--tab SPEC`, `--timeout SEC` | `value`, `by` (`value` or `label`) |
| `browser ID scroll` | `--by N` \| `--edge top\|bottom`, `--tab SPEC`, `--timeout SEC` | `moved`, `before`/`after` |
| `browser ID wait` | `--for load\|idle\|element\|js` (required), `--selector CSS`, `--expr JS`, `--idle-ms MS`, `--tab SPEC`, `--timeout SEC` | `for`, `waited_s` |
| `browser ID screenshot [URL]` | `--out FILE`, `--full` \| `--selector CSS` \| `--clip X,Y,W,H`, `--index N`, `--force`, `--tab SPEC`, `--timeout SEC` | `path`, `width`, `height`, `mode`, `sha256` (IHDR-verified) |
| `browser ID pdf [URL]` | `--out FILE`, `--force`, `--tab SPEC`, `--timeout SEC` | `path`, `bytes`, `sha256` |
| `browser ID js EXPR` | `--tab SPEC`, `--timeout SEC` | `result` (always `verified: false`) |
| `browser ID dialog state\|accept\|dismiss` | `--text V`, `--tab SPEC`, `--timeout SEC` | `open` (state), `handled`/`closed` |
| `browser ID upload PATH` | `--selector CSS` (required), `--tab SPEC`, `--timeout SEC` | `files[]` (`name`, `size`) — the path is a GUEST path |

`--tab SPEC` is `id:<prefix>`, `active`, or a title/URL substring; with no
`--tab` an action runs only when exactly one page tab exists (`tab-ambiguous`
otherwise).

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
| 2 | usage error (argparse on stderr; no JSON on stdout) |
| 130 | interrupted |
