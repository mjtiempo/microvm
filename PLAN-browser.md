# microvm-control browser control — plan (draft)

Status: **IMPLEMENTED (M1–M5).** The vendored core lives in
`microvm_control/browser/`, the subcommands are wired into
`microvm-control browser`, the hermetic suite is
`tests/unit/test_browser_*.py`, and the real-VM acceptance is
`tests/accept/m6-browser.sh`. M6 (frames/media) stays deferred by decision
(§13) — it is an earned milestone, not part of this delivery. The rationale
and companion-doc relationship below are unchanged.

Companion docs: `PLAN.md` (the microVM CLI this extends),
`skills/microvm-control/references/verbs.md` (the surface of record), and
the `browser-control` repo's `docs/plan-remote-cdp.md`
(<https://github.com/mjtiempo/browser-control/blob/main/docs/plan-remote-cdp.md>)
— the *other* route: teach the generic tool to attach to a tunnel. This plan
deliberately does not need that one; §10 says why.

## 1. Goal

`microvm-control browser <ID> <action>` drives the headless Chromium **inside
the guest** with the full verification discipline of the house style: one verb
per process, one JSON object, a mutation is never `ok: true` on a bare
acknowledgement, and every refusal names its code and its fix.

What that buys, over today's one-shot `chromium --dump-dom URL`:

- **State**: click, then read the page the click produced. A running browser
  keeps its DOM, tabs, scroll position and session across calls — the current
  helper starts a cold Chromium per call, so there is no "after".
- **Interaction**: click / type / press / select / check / scroll / hover,
  with the read-back in the reply (`changed`, `url` before and after,
  `checked`, `length_after`).
- **Structure**: `find` (element boxes + hit-test), `extract` (repeated items
  to records, CSS only), `dom`, `js` as the declared escape hatch.
- **Artifacts from the live page**: `screenshot` / `pdf` of the tab as it is,
  not of a fresh load.

Non-goals: windowed mode (the guest has no GPU — headless is the only honest
mode), extensions, plugins, site adapters, the user-profile machinery
(`profile seed/logins`), iframe targeting in v1 (§12), and driving any browser
outside a microVM (that is `browser-control-cli`'s job).

## 2. What already exists

| Asset | Use |
| --- | --- |
| `microvm_control/lifecycle.py::browser` | the one-shot helper; stays the compatibility path, and its guest paths (`/persist/work`, Chromium flags) are the starting point |
| `microvm_control/sshx.py` | key/known-hosts-pinned `ssh`, `run_cmd`; add one `forward()` context manager |
| Guest image (`build.sh`) | Alpine + `chromium` already installed; a browser to drive needs no image change |
| `/persist` | survives reboots — the browser profile and log can live there |
| `tests/accept/m2-clone.sh` | already proves the guest can reach a host fixture server and render it; the new suite extends this |
| [`browser-control`](https://github.com/mjtiempo/browser-control) (MIT) | the CDP transport, DOM verbs, target resolution and read-back logic to extract (§4) |
| `tests/accept/m4-agent.sh` | the docs↔CLI sync check; new verbs and codes must land in `references/verbs.md` / `errors.md` |

## 3. Architecture

```text
microvm-control browser ID <action>
  │
  ├─ 1. state          instances/<id>/browser.json  (guest pid, port, fingerprint)
  │      reconcile: guest `kill -0` + /proc/<pid>/cmdline marker via one ssh
  │
  ├─ 0. detect         the VM must be running; none/stopped → `not-running`
  │      (hint `start <id>`) — nothing here starts a VM
  │
  ├─ 2. ensure         only `browser start` boots, in the guest:
  │      setsid chromium --headless --no-sandbox
  │        --remote-debugging-port=<port>   (default 9222; 0 = the guest picks)
  │        --user-data-dir=/persist/chromium
  │        --no-first-run --disable-extensions
  │        >/persist/work/browser-chromium.log 2>&1 &
  │      read DevToolsActivePort, wait for that port to answer, record the pid
  │
  ├─ 3. transport      ephemeral, per invocation:
  │      ssh -N -L 127.0.0.1:<free>:127.0.0.1:<guest_port>   (per-instance key)
  │      — torn down when the call ends; no host daemon, no stale tunnel
  │
  └─ 4. drive          microvm_control/browser/  (vendored core)
         GET /json/version → identity + fingerprint (against browser.json)
         GET /json/list    → tabs
         WS → Target/Page/Runtime/DOM/Input/Emulation/…
         read-back → envelope(verified, effect, data)
```

Three decisions carry the design:

1. **The CDP client runs on the host.** The guest needs nothing new — no
   Python, no daemon, no open port on the tap. The guest keeps exactly one
   artifact: a headless Chromium process with loopback DevTools.
2. **The tunnel is ephemeral.** Every invocation opens its own `ssh -N -L`,
   uses it, and kills it. There is no tunnel pid to reconcile, no local port
   to leak, and a dead tunnel is just the next call opening a new one. The
   cost is one extra ssh handshake (~100 ms on the tap), which the existing
   per-call `ssh` already pays.
3. **Identity is verified in the guest, not by TOFU.** Unlike a stranger's
   tunnel, this CLI has root SSH into the guest, so the kernel oracle is
   available one level down: the recorded guest pid must be alive **and** its
   `/proc/<pid>/cmdline` must still carry this profile and this port. The
   `/json/version` fingerprint is then a second, independent gate against port
   takeover. This is stronger than `browser-control-cli`'s remote plan (§10),
   and it exists precisely because microvm-control owns the guest.

## 4. Extraction map — what comes from `mark-browser-control`

Copy (MIT, keep the copyright header and a provenance line in each file),
adjusting import paths and the error vocabulary; keep the upstream module
layout so later fixes can be cherry-picked. Nothing in `lib/browser/machine`,
`owners`, `attachments`, `locks`, `profile` or `plugins` is taken — microvm
has its own equivalents, and the host `/proc` guard is replaced by the
in-guest check (§3).

| Upstream | LOC | Disposition |
| --- | --- | --- |
| `lib/cdp/rpc.py` | 352 | **Split.** The I/O loop, deadlines, result caps, one-connection-per-verb, "reads retry / mutations never replay" → `browser/cdp.py`. The `websockets` dependency → `browser/ws.py`, a stdlib RFC 6455 client (§9) |
| `lib/cdp/endpoint.py` | 385 | **Adapt** → `browser/endpoint.py`: bounded `/json` HTTP, redirect refusal, `/json/version`, `/json/list`. Drop `DevToolsActivePort` (we record the port) |
| `lib/cdp/session.py` | 393 | **Adapt** → `browser/session.py`: `Page.enable`, `Runtime.evaluate`, screenshot plumbing |
| `lib/cdp/targets.py` | 125 | **Copy** → `browser/targets.py`: `id:<prefix>` / `active` / title-URL substring resolution; two matches refuse |
| `lib/dom/queries.py` | 728 | **Copy** → `browser/dom.py`: `text`, `find`, `extract` |
| `lib/dom/actions.py` | 782 | **Copy** → `browser/dom.py`: `click`, `hover`, `check`, `select`, `scroll`, focus |
| `lib/dom/text_input.py` + `keys.py` | 349 | **Copy** → `browser/input.py`: `insert` (atomic `Input.insertText`), `type` (real key events), `press` |
| `lib/dom/screenshot.py` | 241 | **Adapt** → `browser/screenshot.py`: keep the PNG-IHDR oracle and the four modes; write the file host-side |
| `lib/dom/dialog.py`, `scroll.py` | 434 | **Copy** → `browser/dom.py` |
| `lib/dom/pagedata.py`, `result.py` | 108 | **Copy** → `browser/dom.py`: reply shapes, bounds |
| `lib/browser/readback.py` | 355 | **Copy** → `browser/readback.py`: verdict helpers (url/title/scroll antes y después) |
| `lib/dom/frames.py`, `media.py` | 828 | **Drop (v1)** → deferred: iframe targeting and media verbs. Top document + open shadow roots only, stated in the skill |
| `lib/browser/{machine,owners,lifecycle,selector,nav,tabs}` | ~3 000 | **Drop**: host census, `/proc` ownership, launch/close/profile — microvm's own state, ssh and lifecycle replace them |
| `lib/{attachments,locks,profile,plugins,policy,capabilities}` | ~2 000 | **Drop**: no attachments, no policy gate, no seeded logins in a disposable VM |

Rough size of the vendored core: **~2 700 LOC adapted**, landed in phases
(§12), each phase copying only what it needs.

Vocabulary note: the refusal *names* stay upstream's (`no-page-tab`,
`tab-ambiguous`, `no-match`, `occluded`, `wait-timeout`, `eval-timeout`, …) so
the two codebases read alike; they are raised through `MvError`, so the m4
docs-sync check and the `ERR[code]` contract apply unchanged.

## 5. State model

One new file per instance, next to `meta.json`: `instances/<id>/browser.json`.
It exists because **one `microvm-control` invocation is one process** — a later
`browser text` has no memory of the `browser start` before it. This is the
handoff: what the controlled browser is (guest pid), where it answers
(resolved port), and how it proves it is still the same browser (fingerprint).
It is `meta.json`'s sibling, and like `meta.json` it is a *lead*, never a
fact — every read reconciles it against the guest before anything is driven.

```json
{
  "schema": 1,
  "pid": 4711,                    // guest pid
  "port": 9222,                   // RESOLVED DevTools port (a fact, not a request)
  "port_requested": 9222,         // what `browser start` asked for; 0 = dynamic
  "profile": "/persist/chromium",
  "started_at": "2026-10-17T09:12:00Z",
  "log_path": "/persist/work/browser-chromium.log",
  "fingerprint": {"browser": "HeadlessChrome/153.0.0.0", "protocol": "1.3",
                  "user_agent": "Mozilla/5.0 …", "v8": "15.3"}
}
```

A clone or backup carries the file away; the pid in it is then stale, which is
exactly what reconciliation is for.

Reconciliation is the `meta.json` rule reapplied: a recorded pid is a *lead*,
never a fact. Every browser verb does one cheap ssh (`cat /proc/<pid>/cmdline`
plus a `kill -0`), and a pid that is gone or no longer carries
`--user-data-dir=/persist/chromium` with a `--remote-debugging-port` is stale:
`browser.json` is cleared, and the verb refuses with `browser-not-running`
and the hint `microvm-control browser <id> start` — the only verbs that start
anything are `browser start` and `browser restart`.

**No action auto-starts anything**, at either level. Before touching the
guest, a browser verb checks the VM: an unknown or stopped instance refuses
`not-found` / `not-running` with the existing hints (`list`, `start <id>`).
Then it checks the browser state: a stopped one refuses `browser-not-running`
with the `browser <id> start` hint. Detection is the notification — the
refusal carries the code, the message and the fix, and the caller decides
what to do.

The profile is the image's own `/persist/chromium` — what
`/root/.config/chromium` symlinks to — so the controlled browser shares
cookies and logins with anything else run against that profile, and they
survive reboots. The flip side is Chromium's profile singleton lock: a
human-run `chromium` on the same profile and the controlled browser cannot
both be up, and `browser start` reports that as `browser-start-failed` naming
the profile instead of retrying around it.

Lifecycle:

| verb | semantics |
| --- | --- |
| `browser ID start [--port N]` | default 9222, `--port 0` lets the guest pick; the actual port is read from `DevToolsActivePort` and recorded; `already_running: true` is a valid reply; wait for the port + fingerprint before `verified: true` |
| `browser ID stop` | SIGTERM the guest pid, wait for it to be gone; `unconfirmed` if it needed a second signal; clears `browser.json` |
| `browser ID restart` | stop then start |
| `browser ID status` | pid, port, profile, uptime, tab count, fingerprint; reconciles first, never lies |
| `delete` | removes `browser.json` with the instance directory — explicitly, before the move to trash, so a trashed instance never claims a live browser. `reap` (which deletes) does the same; `gc` only touches trash |
| VM `stop` | does **not** touch `browser.json`. The guest (and its Chromium) dies with the VM; the recorded pid is then dead, and the next `browser` verb reconciles it to `browser-not-running` — the same rule as a crashed browser |

## 6. Transport and identity, precisely

```text
sshx.forward(key, known_hosts, ip, guest_port, timeout) -> context manager
  1. locate a free loopback port on the host (bind 0, close, reuse — retry once)
  2. ssh -F /dev/null -N -L 127.0.0.1:<local>:127.0.0.1:<guest_port>
     (the same pinned options sshx already uses: BatchMode, ConnectTimeout,
      StrictHostKeyChecking=yes, IdentitiesOnly)
  3. poll 127.0.0.1:<local> until accept, deadline-bounded
  4. yield <local_port>; on exit, terminate and reap the ssh process
```

The websocket URL from `/json/version` is host-checked to loopback (the
upstream `_checked_ws` rule, kept verbatim) — with the tunnel, everything the
client speaks to is 127.0.0.1 by construction.

The guest forward port is a fact read from the guest, not the request:
`browser start` launches with `--remote-debugging-port=<N>` (default 9222,
`--port 0` for dynamic) and then reads `<profile>/DevToolsActivePort` over ssh
— first line, the port the browser actually opened. The requested port is
only the fallback when the file is absent, and the endpoint must answer before
it is recorded. Every later call tunnels to the recorded port.

The identity gate, in order, all before any page traffic:

1. **Guest process**: pid alive and its own cmdline carries the recorded
   profile + port (`browser-identity-changed` otherwise). This is the
   microvm-specific kernel oracle.
2. **Fingerprint**: `/json/version` `Browser` + `Protocol-Version` +
   `User-Agent` + `V8-Version` equal `browser.json`'s
   (`browser-identity-changed` with both values named).
3. **Family**: the answer is Chromium-family (`browser-start-failed` at start,
   `cdp-error` on a later call).

A stale record, a rebooted guest, a second chromium, or a local process that
took the tunnel's port all land on a named refusal; none of them is driven.

## 7. Command surface

One noun, one action per process. `browser ID` takes a subcommand; the three
legacy action flags stay (§7.4). Every action requires a running VM and a
running browser (§5) — none of them boots either one; `browser start` is the
one verb that starts the browser, and the VM is `microvm-control start`'s job.

### 7.1 Lifecycle

| command | data on success |
| --- | --- |
| `browser ID start [--port N]` | `pid`, `port` (resolved), `port_requested`, `profile`, `fingerprint`, `started_ms`, `already_running` |
| `browser ID stop` | `pid`, `graceful`, `freed`: whether the pid and the port are gone |
| `browser ID restart` | as `start` |
| `browser ID status` | `running`, `pid`, `port`, `uptime_s`, `tabs`, `profile`, `fingerprint`, `log_path` |

### 7.2 Actions

| command | flags | verified by |
| --- | --- | --- |
| `open URL…` | `--timeout` | tab row re-read; the URL's `loaded` state |
| `tabs` | — | `/json/list` shape, one row per page tab |
| `nav URL` | `--tab SPEC` | observed URL + `readyState`, not an error page |
| `back` `forward` `reload` | `--tab SPEC` | the document moved / `performance.timeOrigin` changed |
| `text` | `--tab --selector --chars N` | the page's rendered text, truncated in the page |
| `find TEXT\|--selector CSS` | `--tab --cap N` | rect + visibility + real hit-test |
| `extract --each CSS --field NAME=SPEC` | `--tab --cap N --chars N` | per-item records, bounded at the page |
| `dom [URL]` | `--tab --out FILE` | rendered DOM; large output spills to `out_path` with sha256, like `exec` |
| `click TEXT\|--selector CSS` | `--tab --index N --at X,Y` | hit-test before + url/title/scroll before-and-after (`changed`) |
| `hover` `focus` `press KEY` | `--tab`, selector/text where applicable | `:hover` / `activeElement` / L1 + note |
| `insert TEXT` `type TEXT` | `--tab` | the focused field's text length grew, never the value |
| `check` `select` | `--tab --index N`; select takes `--value V` | the control's own `checked` / `value` |
| `scroll` | `--by N \| --edge top\|bottom` | polled scroll position |
| `wait --for load\|idle\|element\|js` | `--timeout --selector --expr --idle-ms` | the predicate itself, polled to a deadline |
| `screenshot` | `--out FILE` `--full\|--selector CSS\|--clip X,Y,W,H` `--force` | the PNG's own IHDR against the region × `devicePixelRatio` |
| `pdf` | `--out FILE` | `%PDF` magic + byte count (Chromium does the layout, so this is L2) |
| `js EXPR` | `--tab` | declared escape hatch: the page's own value, `verified: false` |
| `dialog state\|accept\|dismiss` | `--text` | the renderer answering again |
| `tab close SPEC…\|--all` | `--tab` | the id absent from the re-read list |
| `upload GUEST_PATH` | `--selector CSS` | `input.files` name + size read back (v1: guest-side paths only — that is where the file already is) |

Addressing: `--tab SPEC` is `id:<prefix>`, `active`, or a title/URL substring.
With no `--tab`, a page action runs only when exactly one page tab exists; two
refuse `tab-ambiguous` naming both. One verb, one connection, one deadline
(`--timeout`, default 60 s, mutations never replayed).

### 7.3 Replies

The existing envelope, unchanged:

```json
{"schema": 1, "ok": true, "verified": true, "effect": "confirmed",
 "rung": "cdp",
 "instance": {...},
 "data": {"action": "click", "clicked": true, "changed": true,
          "before": {"url": "...", "title": "...", "scroll": 0},
          "after":  {"url": "...", "title": "...", "scroll": 412},
          "tab": "id:2D4BC76C"}}
```

`verified: false` stays honest where the oracle cannot know (`js`, `press`),
with a `note` telling the caller what to read next. `error.detail` carries the
evidence (candidates, the holder, the two fingerprints).

### 7.4 Compatibility

`browser ID --dom URL`, `--screenshot URL --out FILE`, `--pdf URL --out FILE`,
`--window-size` keep their exact current behavior and one-shot semantics, so
`m2-clone.sh`, the README and the skill examples keep working. They are
documented as the *simple* path; the subcommands are the *capable* path. A
future release may route them through the controlled browser once that is
strictly better (screenshot of a stateful page is), but not in this plan —
compatibility first.

Surface note: this is the existing `browser` verb growing subcommands — the
entry point decided in §13 — not a new binary.

## 8. Refusal codes

New codes, in `MvError` form, documented in `references/errors.md`:

| group | codes |
| --- | --- |
| lifecycle | `browser-start-failed`, `browser-not-running`, `browser-identity-changed` |
| transport | `cdp-unreachable`, `cdp-error`, `eval-timeout`, `result-too-large`, `blocked` |
| addressing | `no-page-tab`, `tab-ambiguous`, `no-match`, `ambiguous-element` |
| navigation | `nav-failed`, `nav-not-verified` |
| interaction | `occluded`, `no-focus`, `insert-not-verified`, `type-not-verified`, `check-not-verified`, `select-not-verified`, `not-a-select`, `scroll-not-verified`, `focus-not-verified`, `hover-not-verified` |
| waits/artifacts | `wait-timeout`, `file-exists`, `write-failed`, `screenshot-not-verified`, `js-error`, `no-dialog` |
| upload (M4) | `no-file`, `upload-not-verified` |

`not-running` (the VM itself) and `bad-args` are reused. Every mutation's
`effect` follows the existing contract (`confirmed` / `unconfirmed` /
`unverifiable`), and `exec`-style big-output handling (spill to `out_path` +
sha256) is reused for `dom`/`extract` replies.

## 9. Dependency strategy — stdlib only

`microvm_control` imports nothing outside the standard library today, and the
launcher runs from a bare checkout with `PYTHONPATH`. `browser-control-cli`
uses the `websockets` package; **this plan replaces it with a vendored
RFC 6455 client** in `microvm_control/browser/ws.py` (~250 lines): handshake,
text/binary frames, masking, fragmentation reassembly, ping/pong, close
handshake, a hard frame cap, and socket deadlines. No TLS (the peer is always
loopback over ssh).

The risk is real — WebSocket is easy to get subtly wrong — so it is fenced:

- a fake server in `tests/unit` that fragments every message, pings mid-stream,
  closes mid-frame, sends oversized frames and non-UTF-8 text;
- the real guest Chromium in `tests/accept/m6-browser.sh` (a version that
  fragments nothing still proves the happy path end to end);
- the alternative (`dependencies = ["websockets"]` + a host install step) is
  recorded here as *rejected for now*: it turns a checkout-and-run tool into a
  pip-managed one, and the host Python is not the tool's only consumer.

MIT attribution: each vendored file keeps the upstream copyright line, and a
`microvm_control/browser/NOTICE` names the source repository and license.
`README.md` gets one line under Layout.

## 10. Why not `browser-control-cli` remotely

The companion plan
(<https://github.com/mjtiempo/browser-control/blob/main/docs/plan-remote-cdp.md>)
would let the generic tool `attach --cdp-url` a tunnel, with the endpoint's
self-reported fingerprint as its only identity oracle (TOFU). That is the
right shape for a tool that does not own the machine on the far side; it is
strictly weaker than this plan, which can check the guest's own `/proc`.
Vendoring also keeps the JSON envelope, audit log, docs-sync test and
zero-dependency install inside this repo, and makes the guest the only
runtime requirement.

If the other plan lands first, `microvm-control browser` could *optionally*
delegate its drive step to an installed `browser-control-cli` — noted as a
future integration, not a dependency (the vendored path must keep working
without it).

## 11. Testing

| tier | file | cases |
| --- | --- | --- |
| unit | `tests/unit/test_browser_ws.py` | handshake, masking, fragmentation, interleaved ping, close, oversized frame, deadline, non-UTF-8 |
| unit | `tests/unit/test_browser_cdp.py` | fake `/json/version` + `/json/list` + fake WS peer: call/evaluate, CDP error envelope, result cap, "reads retry / mutations never replay" |
| unit | `tests/unit/test_browser_state.py` | `browser.json` reconciliation: dead pid, pid reuse (cmdline mismatch), port taken by a stranger, malformed state |
| accept | `tests/accept/m6-browser.sh` | rootless, real VM: `start` → `open` host fixture (the m2 HTTP server on the tap gateway) → `text` marker → `click` link → `nav` verified → `screenshot` IHDR + PNG magic → `extract` records → `tabs` → `js` → kill guest chromium → next `status` reconciles dead → `start` again → `stop` |
| accept | `m4-agent.sh` (extend) | every new top-level `browser` subcommand appears in `verbs.md`; every new code appears in `errors.md` (the existing regex counts `MvError("…")` literals) |

Performance budget, asserted loosely in m6: a `text` on a warm browser under
2 s wall-clock including ssh, tunnel setup, CDP and teardown. If the ephemeral
tunnel blows that budget, the fallback is a parked tunnel per instance in
`browser.json` (pid + local port) — measured before it is built, per house
rules.

## 12. Milestones

| # | Deliverable | Done when |
| --- | --- | --- |
| M1 | `browser/ws.py` + `cdp.py` + `endpoint.py` + fake-server unit tests | the fake peer passes the torture tests; `GET /json/version` parses against a fake |
| M2 | `browser start\|stop\|status\|restart`, `browser.json`, `DevToolsActivePort` resolution, guest `/proc` reconciliation, `sshx.forward`, identity gate | m6 proves start/stop/reconcile against the real guest; no tunnel process survives a call |
| M3 | `open`, `tabs`, `nav/back/forward/reload`, `text`, `dom`, `screenshot`, `pdf` | m6 drives the fixture page and captures a verified PNG |
| M4 | `click`, `hover`, `focus`, `press`, `insert`, `type`, `check`, `select`, `scroll`, `wait` | read-backs asserted in m6 for at least click/nav/insert |
| M5 | `find`, `extract`, `js`, `dialog`, `tab close`, `upload` (guest paths) | `extract` returns records from the fixture; docs + skill + errors updated; m4 passes |
| M6 | (earned only) iframe targeting + media verbs from the deferred upstream files; optional delegation to `browser-control-cli` | an explicit ask, not a default |

Each milestone includes its own `references/verbs.md` entries and code doc
lines in the same change — the m4 sync test fails otherwise, which is the
point.

## 13. Decisions and open questions

Taken in this review:

1. **Entry point — subcommands.** `microvm-control browser ID <action>` on
   the existing verb; no separate `microvm browser` binary.
2. **No auto-start — detect, then notify.** No browser verb starts the VM or
   the browser. Each checks: VM running? (`not-running`, hint `start <id>`),
   browser running? (`browser-not-running`, hint `browser <id> start`). The
   refusal IS the notification; `browser start`/`restart` are the only verbs
   that boot the browser.
3. **Vendored layout — `microvm_control/browser/`.** One package inside this
   repo; the shared-standalone-package idea is dropped with it.
4. **Profile — `/persist/chromium`.** The image's own profile (the
   `/root/.config/chromium` symlink target), shared with anything else run
   against it, logins surviving reboots. The singleton-lock contention with a
   human-run guest Chromium is accepted and reported as `browser-start-failed`.
5. **Port — 9222 default, dynamic supported, `DevToolsActivePort` is the
   truth.** `browser start --port N` requests (default 9222); the guest's
   actual port is read from the profile's `DevToolsActivePort` and recorded in
   `browser.json`, so `--port 0` works and a taken 9222 is visible rather than
   guessed.
6. **`delete` clears `browser.json`, VM `stop` does not.** `delete`/`reap`
   remove the file with the instance; `stop` leaves it and lets the next
   `browser` verb reconcile the dead guest pid.

Still open:

1. **Frames — confirm the v1 scope.** What is dropped: no `--frame` on any
   page verb, no frame census, no cross-origin iframe targets. Every DOM verb
   reads and acts in the top document + open shadow roots, and the skill says
   so. What M6 would restore, from upstream `lib/dom/frames.py` (~560 LOC) and
   the frame-aware plumbing: a `frames` listing, `--frame <index|URL
   substring>` on content verbs, cross-origin frames driven as their own CDP
   target, and same-process frames read through the page's own
   `contentDocument` (no `tab js` needed). The pending bit is a confirmation
   that no near-term consumer acts inside an iframe — embedded editors,
   payment/iframe forms, embedded previews; reading frame text is the common
   ask and the first thing M6 should land.

## 14. Risks

| risk | mitigation |
| --- | --- |
| Vendored WS client has a protocol bug | torture tests + real-Chromium m6; the failure mode is a bounded refusal (`cdp-error` / `blocked`), never a wrong page action |
| Fork drifts from `mark-browser-control` | parallel module layout + provenance headers + the extraction map table as the contract; cherry-pick upstream fixes by module |
| Alpine Chromium upgrades change CDP behaviour | record `Protocol-Version` at start; a version-gated capability probe before M3+ verbs; the one-shot legacy path is the fallback for a broken environment |
| Chromium OOM/CPU in a small VM | document `create --mem` for heavy pages; `browser status` reports the process; `browser stop` is the reset |
| Ephemeral tunnel latency | measured in m6 against the 2 s budget; parked-tunnel fallback designed in §11 |
| Agents screenshot/screenshot-crop in a loop | per-call `--timeout` and output caps; artifacts land under the instance dir like `exec` artifacts, purged by `gc` |
| CDP has no authentication in the guest | the port is loopback-only inside the guest; the tunnel is authenticated by the per-instance SSH key; nothing is bound to the tap |
