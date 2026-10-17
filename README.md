# microVM: Firecracker + terminal + browser + persistent disk

A minimal, reproducible setup for spinning up a Firecracker microVM with:

- **Terminal** — SSH (key auth) plus a serial console on stdout
- **Browser** — headless Chromium (screenshots, PDF, DevTools automation) plus `w3m` for terminal browsing
- **Persistent filesystem** — a second ext4 drive at `/persist`, mounted at boot

No host root is needed to build the images (a user namespace is used for
staging, so the guest sees proper root-owned files). Root is only needed once
to create the tap device + NAT.

## Control CLI — `microvm-control`

`microvm-control` is the agent-facing entry point: one verb per process, one
JSON object per call, `ERR[code]` refusals (exit 1, usage 2), `--dry-run`, and
state under `~/.local/share/microvm` (`MICROVM_HOME` overrides). The agent
skill and full references live in `skills/microvm-control/` (installed at
`~/.pi/agent/skills/microvm-control`).

```bash
microvm-control image build                # once: build/import the base image set
microvm-control doctor | jq .data.checks   # preflight
microvm-control create demo --ttl 3600     # reflinked disks, per-instance ssh key + host keys
microvm-control start demo                 # boots via the API, waits for ssh readiness
microvm-control exec demo -- uname -a      # exit_code is data, not a refusal
microvm-control browser demo --screenshot https://example.com --out shot.png
microvm-control cp demo:/etc/alpine-release ./alpine-release
microvm-control clone demo demo2           # running source: pause/copy/resume
microvm-control snapshot create demo --name s1 && microvm-control snapshot load demo --name s1
microvm-control backup demo --name nightly # tar.zst + sha256 manifest
microvm-control restore <home>/backups/demo-nightly.tar.zst --id demo3 --verify
microvm-control logs demo --tail 50
microvm-control status demo --probe
microvm-control stop demo && microvm-control restart demo
microvm-control list | jq .data.instances
microvm-control reap && microvm-control gc --snapshot-days 7
microvm-control delete demo --yes          # moves to ~/.local/share/microvm/trash
microvm-control audit --tail 20            # JSONL of every invocation
```

State lives under `~/.local/share/microvm` (mode 0700, enforced for every
verb, including read-only ones): `audit.log` is 0600, records argv
(`MICROVM_AUDIT_ARGS=0` omits it), and is opened and chmodded through one
`O_NOFOLLOW` descriptor. `sudo microvm-control net install` hands
the pool state back to the invoking user — only when the state dir really sits
inside their home, and never through a symlink; `doctor` flags a root-owned
`net.json` with the exact `chown` fix. Each instance gets its own SSH key at
create/clone time, so the image-baked control key is no longer authorized
inside CLI-created instances (the legacy `run.sh`/`ssh.sh` flow still uses it).
`./build.sh` verifies the Firecracker tarball and the Alpine minirootfs against
their published sha256, and `apk.static` against the APKINDEX checksum before
executing it (the CI bucket publishes no kernel checksum, so its digest is
recorded on first download and re-checked whenever it is reused).

### Networking: tap pool (M1)

One-time, with sudo — creates `mvtap1..N` (`/30` each), static NAT and a
`microvm-net.service` systemd unit so the pool survives reboots:

```bash
sudo microvm-control net install --taps 8
microvm-control net status | jq .data     # taps, allocations, service, NAT
sudo microvm-control net grow --taps 12   # resize later
sudo microvm-control net uninstall        # remove unit, taps and pool state
```

`create` then auto-allocates a free tap/`/30` per instance (pool exhausted →
`ERR[no-tap-available]`), and `delete` releases it; several microVMs run
concurrently. Without a pool, `create` falls back to the shared `fc-tap` and
refuses a second concurrent instance (`net-conflict`). `microvm-control net
init --taps 8` defines the pool in state without root (useful for tests and
for inspecting allocation before installing).

Acceptance suites run the real stack rootless (private user+network
namespace): `tests/accept/m0.sh` (shared-tap lifecycle), `m1-pool.sh` (pool,
two concurrent VMs), `m2-clone.sh` (per-instance keys, snapshots, cold/live
clone, browser, reap), `m3-backup.sh` (backup/restore, corrupt-archive
refusal, qcow2) and `m4-agent.sh` (docs↔CLI sync, audit, reply shapes).

## Quickstart (shell scripts)

```bash
./build.sh          # download firecracker/kernel/alpine, build ext4 images
sudo ./net.sh up    # create tap 'fc-tap' (172.16.0.1/24) + NAT for outbound traffic
./run.sh            # boot; guest serial console attached to this terminal
./ssh.sh            # in another terminal: ssh root@172.16.0.2 (key auth)
                    #   ./ssh.sh 'cmd' runs a remote command
                    #   ./ssh.sh -L 9222:127.0.0.1:9222 tunnels DevTools
./test.sh           # optional: rootless smoke test (boot, ssh, chromium, persistence)
```

`run.sh` foreground: `reboot` inside the guest exits the VM cleanly, `Ctrl-C`
kills it hard. Afterwards `sudo ./net.sh down` tears the network down.

Serial console login is `root` with the password set at build time
(`VM_PASSWORD=...`, default `microvm`). SSH is key-only (`keys/id_ed25519`,
generated by `build.sh`) for the legacy flow; `microvm-control` instances use
per-instance keys.

## Browser

**Controlled browser.** `microvm-control browser <id> <action>` drives a
long-running headless Chromium in the guest over CDP — navigate, read text,
click, type, wait, extract records, screenshot/PDF, all with the house
read-back contract. `browser <id> start` boots it (nothing auto-starts),
`browser <id> status` reconciles the recorded guest pid and endpoint
fingerprint, and `browser <id> stop` ends it. The core (WebSocket + CDP +
DOM verbs) is vendored in `microvm_control/browser/` from
`mark-browser-control` (MIT) — see `PLAN-browser.md`. The launch carries a windowed user-agent (built from the guest binary's own
version) and `--disable-blink-features=AutomationControlled`, so a strict bot
check does not see `HeadlessChrome` or `navigator.webdriver`. Nothing is
exposed on the tap: each call opens an ephemeral `ssh -N -L` to the guest's
loopback DevTools port and tears it down.

```bash
microvm-control browser demo start
microvm-control browser demo open https://example.com
microvm-control browser demo text --chars 500 | jq -r .data.text
microvm-control browser demo click "Learn more" | jq .data.changed
microvm-control browser demo screenshot --out page.png
microvm-control browser demo stop
```

Terminal browsing:

```bash
w3m https://example.com
```

Chromium is installed and meant to be used headless — Firecracker has no GPU
or display device, and headless mode needs no X server. `--no-sandbox` is
required because commands run as root in the guest:

```bash
chromium --headless --no-sandbox --dump-dom https://example.com
chromium --headless --no-sandbox --screenshot=/persist/work/shot.png \
  --window-size=1280,800 https://example.com
chromium --headless --no-sandbox --print-to-pdf=/persist/work/page.pdf \
  https://example.com
```

For automation, expose the DevTools protocol and tunnel it to the host:

```bash
chromium --headless --no-sandbox --remote-debugging-port=9222 &   # in the guest
./ssh.sh -L 9222:127.0.0.1:9222                                   # on the host
curl http://localhost:9222/json/version
```

Then point Playwright (`connectOverCDP('http://localhost:9222')`), Puppeteer or
any other CDP client at the tunnel. The Chromium profile lives in
`/root/.config/chromium`, symlinked to `/persist/chromium`, so cookies and
logins survive reboots. Add WebDriver with
`EXTRA_PKGS=chromium-chromedriver ./build.sh` if you need it.

## Persistent filesystem

Two drives are attached (`vm.json`):

| drive   | host file             | guest                   | rebuilt by `./build.sh` |
| ------- | --------------------- | ----------------------- | ----------------------- |
| rootfs  | `images/rootfs.ext4`  | `/dev/vda` → `/`        | yes (OS is disposable)  |
| persist | `images/persist.ext4` | `/dev/vdb` → `/persist` | no, never               |

So OS changes survive reboots, and `/persist` survives OS rebuilds too. These
paths already point onto the persistent disk:

- `/root/.w3m` → `/persist/w3m` (history, cookies, bookmarks)
- `/root/.config/chromium` → `/persist/chromium` (Chromium profile)
- `/root/work` → `/persist/work`

Any missing subdirectory is created at boot by `/etc/local.d/10-persist.start`,
so an existing `persist.ext4` keeps working after the guest image changes.

To reset only user data: `rm images/persist.ext4` and run `./build.sh` again
(recreates the filesystem layout). Never attach the same image to two running
VMs — ext4 is not cluster-safe.

## Layout

```text
microvm-control      CLI launcher (repo on PYTHONPATH)
microvm_control/     python package: store, fcapi, identity, netpool,
                     lifecycle, backup, sshx, audit, __main__
skills/microvm-control/  agent skill + references/verbs.md + references/errors.md
tests/accept/        m0..m5 rootless acceptance suites (+ lib.sh)
tests/unit/          unittest suite for the internals
scripts/fetch-verified.sh   checksum-verified downloads for build.sh
build.sh net.sh run.sh ssh.sh test.sh   legacy shell flow (still working)
vm.json              firecracker config for the legacy flow
images/ keys/ run/   legacy build artifacts
```

## Tuning

- CPU/RAM: edit `vcpu_count` / `mem_size_mib` in `vm.json`
- Image sizes: `ROOTFS_SIZE=2G PERSIST_SIZE=8G ./build.sh` (persist only
  changes when recreated)
- Versions: `FC_VERSION=v1.17.0`, `ALPINE_VERSION=3.24.2`,
  `ALPINE_BRANCH=v3.24`, `VM_PASSWORD=...` (unset = latest releases)
- Clean slate: `rm -rf images keys run build && ./build.sh`
- Snapshots, clone, backup/restore and the browser helper are provided by
  `microvm-control` (see above); the shell flow keeps a single running VM on
  `fc-tap`.

## Caveats

- Dev setup: no `jailer`, no seccomp hardening, no host firewall rules beyond
  the tap-subnet NAT rules. Don't expose this guest to untrusted workloads.
- The tap subnet `172.16.0.0/24` must not clash with an existing host route
  (override with `TAP=`/`HOST_IP=` in `net.sh` + `vm.json`).
- Requires `/dev/kvm`, `unshare` user namespaces, and `iptables` on the host.
