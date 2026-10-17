# microvm-control refusal codes

`ERR[code]: message` on stderr; `error.code` in the JSON envelope. Branch on
the code, not the message. `error.detail` carries structured evidence when
present (failed hashes, API fault message, tap lists, …).

## State and lookup

| code | meaning / fix |
| --- | --- |
| `not-found` | instance, snapshot or archive does not exist. `microvm-control list` / `snapshot list <id>` |
| `already-exists` | the id (or snapshot name) is taken. Delete it or pick another id |
| `not-running` | the verb needs a running instance. `microvm-control start <id>` |
| `bad-state` | `meta.json` / `net.json` is unreadable, or a privileged verb's state dir resolves outside the invoking user's home (symlinked `--home`). Inspect the file; re-init the pool or delete the instance |
| `invalid-id` | id/snapshot name must match `[a-z0-9][a-z0-9-]{0,30}` |
| `bad-args` | flag combination is invalid (e.g. `--tap` without `--ip`); the hint shows the accepted form |
| `destructive-needs-yes` | `delete` without `--yes` while stdin is not a TTY. `net uninstall` takes no `--yes` (it is root-gated) |
| `quota-exceeded` | the instance limit is reached (default 8, or `max_instances` in `<home>/config.json`). Delete an instance or raise the limit |
| `locked` | cannot open an instance or net lock file |

## Filesystem and images

| code | meaning / fix |
| --- | --- |
| `io-failed` | a filesystem operation failed; `error.detail` has the OS error |
| `read-failed` | could not read a file while hashing it |
| `copy-failed` | disk copy failed (space or permissions) |
| `disk-full` | not enough free space for a staging copy |
| `image-missing` | base image missing/incomplete, or an instance records a runtime outside `<home>/images`. `microvm-control image build` |
| `image-build-failed` | `build.sh`, `mke2fs`, or tar failed; see `<home>/logs/image-build.log` |
| `image-error` | a `debugfs` command failed on the image |
| `image-read-failed` | `debugfs` could not read a file out of the image |
| `image-write-failed` | `debugfs` write failed or the read-back did not match |
| `image-fsck-failed` | `e2fsck` found uncorrectable problems (rc >= 4) |
| `identity-failed` | `ssh-keygen` or the identity read-back failed |
| `delete-failed` | the instance directory could not be removed or trashed |

## Firecracker and API

| code | meaning / fix |
| --- | --- |
| `api-unreachable` | API socket missing/refused; the instance is probably stopped. `start` |
| `api-timeout` | the API socket did not appear in time; check `status` |
| `api-error` | Firecracker returned HTTP >= 400; `error.detail` carries the fault message |
| `boot-failed` | Firecracker exited before SSH was ready; `logs <id> --tail 50` |
| `resume-failed` | the source could not be resumed after pause (clone/backup/snapshot create); `restart <id>`. A body error carrying `left_paused` in `error.detail` means the same |
| `firecracker-error` | the firecracker binary could not be executed; rebuild images |
| `snapshot-failed` | snapshot files were missing or empty after the API call |
| `snapshot-version-mismatch` | snapshot was taken with a different Firecracker version; rebuild or clone from disk |
| `disk-modified` | a disk image changed size since the snapshot; `snapshot load --force` or rebuild |
| `restore-verify-failed` | a restored instance did not become SSH-reachable under `--verify` |

## SSH and guest

| code | meaning / fix |
| --- | --- |
| `ssh-failed` | the `ssh` binary could not be executed |
| `ssh-timeout` | the guest did not accept SSH within the timeout; `status --probe`, `logs <id>` |
| `exec-timeout` | the remote command exceeded `--timeout`; it may still be running in the guest |
| `scp-failed` | file transfer failed; check the path and that the instance runs |
| `browser-failed` | guest Chromium exited non-zero or produced no artifact (`error.detail` has its stderr) |
| `browser-verify-failed` | the transferred screenshot/PDF is empty or lacks the expected magic bytes |

## Networking

| code | meaning / fix |
| --- | --- |
| `tap-missing` | the instance's tap does not exist. `sudo microvm-control net install` (pool) or `sudo ./net.sh up` (shared tap) |
| `tap-down` | the tap is not up. `sudo ip link set <tap> up` |
| `net-conflict` | another instance is running on the same tap/IP (shared-tap mode). Stop it or install the pool |
| `no-tap-available` | the pool is fully allocated. `sudo microvm-control net grow --taps N` or delete an instance |
| `net-not-configured` | no pool is defined. `microvm-control net init --taps N` (or `sudo … net install`) |
| `pool-shrink` | instances are allocated above the requested pool size; delete them first |
| `net-install-failed` | systemd enable/start failed or taps are not up; `journalctl -u microvm-net.service` |
| `needs-root` | `net install`, `net grow` and `net uninstall` require sudo |
| `missing-tool` | a host tool is required but missing (`tar`, `zstd`, `qemu-img`, `iptables`, `e2fsck`, …) |

## Backup and restore

| code | meaning / fix |
| --- | --- |
| `backup-failed` | archiving failed (tar, `qemu-img`, or a missing artifact) |
| `backup-corrupt` | the archive is unreadable, has unsafe/escaping member names, or a size/sha256 check failed; do not restore it |
| `restore-failed` | decoding or materialising the restored instance failed |

## Browser (CDP in the guest)

| code | meaning / fix |
| --- | --- |
| `browser-not-running` | no controlled browser is running in the instance. `microvm-control browser <id> start` |
| `browser-start-failed` | chromium did not launch, never published `DevToolsActivePort`, or its pid does not carry our profile/port; `error.detail` has the stderr or the log tail (`/persist/work/browser-chromium.log`) |
| `browser-identity-changed` | the recorded guest pid no longer runs our `--user-data-dir`/`--remote-debugging-port`, or the endpoint reports another fingerprint. `browser <id> stop` then `start` |
| `cdp-unreachable` | the endpoint does not answer on the recorded port (the browser died) or the forward failed |
| `cdp-error` | the DevTools protocol refused the call; `error.detail` carries the protocol error object |
| `cdp-not-local` | `/json/version` names a websocket off loopback — refused, nothing sent |
| `eval-timeout` | the page did not answer within the deadline (a parked renderer). Try `dialog state`, then `nav` to escape |
| `no-page-tab` | no page tab matches (or none are open); `browser <id> tabs` lists them |
| `tab-ambiguous` | several tabs match `--tab`; use `id:<prefix>` or a longer substring |
| `no-match` | nothing matched the TEXT/selector (or `select`'s value — options are in `error.detail`) |
| `ambiguous-option` | several `<option>`s match `select --value`; use the exact value |
| `occluded` | the element's centre is covered; scroll it in, use `--at`, or pick another target |
| `no-focus` | nothing editable is focused; `browser <id> focus` a field first |
| `focus-not-verified` | the focus did not land on the named element |
| `not-checkable` | the control has no `checked` state |
| `check-not-verified` | the control did not reach the requested state |
| `not-a-select` | `select --selector` is not a `<select>` |
| `nav-failed` | the navigation ended on an error page (or a download) |
| `nav-not-verified` | the requested URL was not observed before the deadline |
| `reload-not-verified` | the document did not restart |
| `wait-timeout` | the `wait` predicate never passed; raise `--timeout` |
| `screenshot-not-verified` | the PNG's own IHDR did not match the region × `devicePixelRatio`; nothing was written |
| `file-exists` | the output path exists; `--force` to overwrite |
| `write-failed` | the artifact could not be written (permissions, disk) |
| `no-viewport` | the page reports no viewport (windowless/never shown) |
| `result-too-large` | `js` returned more than the cap; narrow the expression |
| `js-error` | the page expression threw; the message is the page's own |
| `no-dialog` | no JavaScript dialog is showing |
| `no-file` | the guest path does not exist; `cp` it into the guest first |
| `upload-not-verified` | no files landed on the input |
| `close-tab-not-verified` | tabs survived `close`; `error.detail.survivors` names them |

## Other

| code | meaning / fix |
| --- | --- |
| `internal` | unexpected error; re-run with `MICROVM_DEBUG=1` for a traceback |
