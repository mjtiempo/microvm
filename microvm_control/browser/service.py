"""service — the guest browser's lifecycle and the `browser` verb dispatch.

This is the glue the vendored core does not have (upstream owns a host
browser; here the browser lives in a Firecracker guest):

* **lifecycle** — `start` launches headless Chromium over SSH, reads the
  RESOLVED port out of the profile's `DevToolsActivePort`, verifies the
  endpoint's identity, and records `browser.json`; `stop` signals the guest
  pid; `status` reconciles and reports.
* **transport** — one ephemeral `ssh -N -L` per invocation (`sshx.forward`),
  so there is no daemon, no tunnel pid and no local port to leak.
* **identity gate** — the recorded guest pid must still carry our
  `--user-data-dir` and `--remote-debugging-port` in its own cmdline, and the
  endpoint must answer `/json/version` with the stored fingerprint. Anything
  else is `browser-identity-changed` and nothing is driven.
* **no auto-start** — nothing here starts a VM or a browser except
  `browser start` / `restart`; every other verb refuses and says what to run.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .. import sshx, store, util
from ..errors import DryRun, MvError
from ..output import envelope
from . import cdp, dom, state, targets

BROWSER_PROFILE = "/persist/chromium"
BROWSER_LOG = "/persist/work/browser-chromium.log"
DEFAULT_PORT = 9222
START_TIMEOUT = 30.0
BROWSER_TIMEOUT = 60.0
INLINE_LIMIT = 16384


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested)
# --------------------------------------------------------------------------- #

def parse_active_port(text: str) -> int:
    """The first line of `DevToolsActivePort` as a port, 0 when unusable."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            port = int(line)
        except ValueError:
            return 0
        return port if 1 <= port <= 65535 else 0
    return 0


def is_chromium(version: dict) -> bool:
    product = str((version or {}).get("Browser") or "").lower()
    return "chrome" in product or "chromium" in product


def ua_from_version(version: str, machine: str = "") -> str:
    """The UA a DESKTOP run of this version sends, in Chrome's reduced form.

    Ported from `mark-browser-control` (MIT): headless Chrome announces
    itself (`HeadlessChrome/…`), and a site's stricter bot check reads that
    before anything else runs; a windowed run already sends this string, so
    the page sees no change. "" when no version can be read — a UA the binary
    cannot back is worse than the truth.
    """
    match = re.search(r"\b(\d+)\.\d+\.\d+\.\d+\b", version or "")
    if not match:
        return ""
    arch = machine or "x86_64"
    return (f"Mozilla/5.0 (X11; Linux {arch}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{match.group(1)}.0.0.0 "
            "Safari/537.36")


def start_shell(port: int, profile: str, log_path: str,
                url: str = "about:blank", user_agent: str = "") -> str:
    """The guest shell that launches Chromium and prints its pid.

    `--disable-blink-features=AutomationControlled` suppresses the
    `navigator.webdriver` marker the debugging port itself produces, and
    `--user-agent` carries the windowed string when the binary named its
    version; without a readable version the browser keeps its own UA.
    """
    ua_flag = f"--user-agent={shlex.quote(user_agent)} " if user_agent else ""
    return (
        "mkdir -p /persist/work && "
        "setsid chromium --headless --no-sandbox "
        f"--remote-debugging-port={int(port)} "
        f"--remote-debugging-address=127.0.0.1 "
        f"--user-data-dir={shlex.quote(profile)} "
        "--no-first-run --disable-extensions --window-size=1280,800 "
        "--disable-blink-features=AutomationControlled "
        f"{ua_flag}{shlex.quote(url)} "
        f">{shlex.quote(log_path)} 2>&1 < /dev/null "
        "& echo $!"
    )


def _last_int(text: str) -> int:
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return 0


def _uptime_seconds(started_at: str) -> float | None:
    try:
        from datetime import datetime, timezone
        moment = datetime.fromisoformat(str(started_at))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - moment).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# instance, guest and session plumbing
# --------------------------------------------------------------------------- #

def _instance(ns, home: Path) -> tuple[store.Instance, dict]:
    """The VM must be running; a browser verb never starts one."""
    inst = store.Instance(home, ns.id)
    meta = inst.reconcile()
    if not inst.pid_alive(meta):
        raise MvError("not-running", f"instance '{ns.id}' is not running",
                      hint=f"microvm-control start {ns.id}")
    return inst, meta


def _guest_cmdline(inst: store.Instance, ip: str, pid: int) -> str | None:
    """The guest process's own argv, or None when it is gone."""
    result = sshx.run_cmd(
        inst.key, inst.known_hosts, ip,
        ["sh", "-c",
         f"kill -0 {int(pid)} 2>/dev/null && tr '\\0' ' ' < /proc/{int(pid)}/cmdline"],
        timeout=8.0,
    )
    if result["exit_code"] != 0:
        return None
    return result["stdout"].strip()


def _reconcile(inst: store.Instance, ip: str) -> dict | None:
    """Load `browser.json`, drop it when the guest process disagrees."""
    record = state.load(inst)
    if record is None:
        return None
    cmdline = _guest_cmdline(inst, ip, record["pid"])
    if state.stale_reason(cmdline, record["profile"], record["port"]):
        state.clear(inst)
        return None
    return record


@dataclass
class Session:
    """One verified invocation: the local forward, the connection and state."""

    inst: store.Instance
    meta: dict
    local: int
    cdp: cdp.Cdp
    state: dict


@contextmanager
def _session(inst: store.Instance, meta: dict, timeout: float):
    """Reconcile, forward, verify the fingerprint, then yield the session."""
    ip = meta["net"]["ip"]
    record = _reconcile(inst, ip)
    if record is None:
        raise MvError("browser-not-running",
                      f"no browser is running in '{inst.id}'",
                      hint=f"microvm-control browser {inst.id} start")
    budget = float(timeout or BROWSER_TIMEOUT)
    with sshx.forward(inst.key, inst.known_hosts, ip, record["port"],
                      timeout=min(10.0, budget)) as local:
        try:
            info = cdp.version(local, timeout=5.0)
        except MvError as exc:
            if exc.code == "cdp-unreachable":
                raise MvError(
                    "cdp-unreachable",
                    f"the browser in '{inst.id}' does not answer on port "
                    f"{record['port']} — it may have died",
                    hint=f"microvm-control browser {inst.id} stop && "
                         f"microvm-control browser {inst.id} start",
                ) from exc
            raise
        if not is_chromium(info):
            raise MvError(
                "browser-identity-changed",
                f"the endpoint on {ip}:{record['port']} is not a "
                f"Chromium-family browser ({info.get('Browser')!r})",
            )
        current = state.fingerprint_of(info)
        if not state.fingerprint_matches(record.get("fingerprint") or {}, current):
            raise MvError(
                "browser-identity-changed",
                f"the endpoint on {ip}:{record['port']} reports another identity",
                hint=f"microvm-control browser {inst.id} stop then start",
                detail={"changed": state.changed_fields(
                    record.get("fingerprint") or {}, current)},
            )
        connection = cdp.Cdp(local, timeout=budget, version_info=info)
        try:
            yield Session(inst=inst, meta=meta, local=local,
                          cdp=connection, state=record)
        finally:
            connection.close()


def _resolve_tab(sess: Session, spec: str) -> dict:
    """The tab a spec names; `active` is answered by the pages themselves."""
    rows = cdp.page_rows(sess.local)
    if (spec or "").strip() == "active":
        for tab in rows:
            try:
                sid = sess.cdp.attach(tab["id"])
                if sess.cdp.evaluate("document.visibilityState === 'visible'",
                                     session_id=sid):
                    return tab
            except MvError:
                continue
        raise MvError("no-page-tab", "no page tab reports itself visible",
                      detail={"tabs": targets.brief(rows)})
    return targets.resolve(rows, spec or "")


def _reply(sess: Session, data: dict, *, verified: bool = True,
           rung: str = "cdp", action: str = "") -> dict:
    body = dict(data)
    verified = bool(body.pop("verified", verified))
    if action:
        body = {"action": action, **body}
    return envelope(
        instance=sess.inst.summary(sess.meta),
        verified=verified,
        effect="confirmed" if verified else "unverifiable",
        rung=rung,
        data=body,
    )


def _timeout_of(ns) -> float:
    return float(getattr(ns, "timeout_verb", None) or ns.timeout or BROWSER_TIMEOUT)


def _action(ns, home: Path, fn, action: str, *, tab=True):
    inst, meta = _instance(ns, home)
    timeout = _timeout_of(ns)
    with _session(inst, meta, timeout) as sess:
        if tab:
            resolved = _resolve_tab(sess, getattr(ns, "tab", "") or "")
            sid = sess.cdp.attach(resolved["id"])
            data = fn(sess, sid, timeout)
            data = {**data, "tab": resolved["id"], "tab_url": resolved.get("url", "")}
        else:
            data = fn(sess, timeout)
        return _reply(sess, data, action=action)


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

def parse_running(output: str) -> dict | None:
    """The main browser process in `pid cmdline` rows, or None.

    Helpers and renderers inherit `--user-data-dir` (and some inherit the
    debugging flag), so only a process WITHOUT `--type=` — the browser process
    itself — is a candidate to adopt.
    """
    for row in str(output or "").splitlines():
        pid_text, _, cmdline = row.strip().partition(" ")
        if not pid_text.isdigit() or "--type=" in cmdline:
            continue
        match = re.search(r"--remote-debugging-port=(\d+)", cmdline)
        if not match:
            continue
        port = int(match.group(1))
        if 0 < port <= 65535 and state.cmdline_is_ours(cmdline, BROWSER_PROFILE, port):
            return {"pid": int(pid_text), "port": port}
    return None


def _find_running(inst: store.Instance, ip: str) -> dict | None:
    """A browser already running our profile, from the guest's own /proc.

    The state file can be lost (a restore, a tampered pid, a crash between the
    launch and the write) while the process lives on; the guest's process
    table is the truth, and `start` adopts what it finds instead of launching a
    second browser that would only defer to the first and exit.
    """
    script = ("for d in /proc/[0-9]*; do p=${d#/proc/}; "
              "c=$(tr '\\0' ' ' < $d/cmdline 2>/dev/null) || continue; "
              "case \"$c\" in *--user-data-dir=/persist/chromium*) "
              "echo \"$p $c\";; esac; done")
    result = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                          ["sh", "-c", script], timeout=8.0)
    if result["exit_code"] != 0:
        return None
    return parse_running(result["stdout"])


def start(ns, home: Path) -> dict:
    inst, meta = _instance(ns, home)
    requested = int(getattr(ns, "port", DEFAULT_PORT) or 0)
    if requested != 0 and not 1 <= requested <= 65535:
        raise MvError("bad-args", f"--port must be 1..65535 or 0 (got {requested})")
    ip = meta["net"]["ip"]
    with inst.lock():
        existing = _reconcile(inst, ip)
        if existing is not None:
            return envelope(
                instance=inst.summary(meta), verified=True, effect="confirmed",
                rung="fs",
                data={"already_running": True, "pid": existing["pid"],
                      "port": existing["port"], "profile": existing["profile"],
                      "fingerprint": existing.get("fingerprint")},
            )
        if getattr(ns, "dry_run", False):
            raise DryRun([
                f"launch chromium --headless --no-sandbox "
                f"--remote-debugging-port={requested} --user-data-dir={BROWSER_PROFILE}",
                f"read DevToolsActivePort, verify the endpoint, record "
                f"instances/{inst.id}/browser.json",
            ])
        adopted = _find_running(inst, ip)
        if adopted is not None:
            try:
                with sshx.forward(inst.key, inst.known_hosts, ip,
                                  adopted["port"], timeout=10.0) as local:
                    info = _wait_version(local, time.monotonic() + 10.0)
            except MvError as exc:
                raise MvError(
                    "browser-start-failed",
                    f"a browser already runs on {BROWSER_PROFILE} "
                    f"(pid {adopted['pid']}) but its endpoint did not answer: "
                    f"{exc.message}",
                ) from exc
            record = state.new_state(
                pid=adopted["pid"], port=adopted["port"],
                port_requested=requested, profile=BROWSER_PROFILE,
                log_path=BROWSER_LOG, fingerprint=state.fingerprint_of(info),
            )
            state.save(inst, record)
            return envelope(
                instance=inst.summary(meta), verified=True, effect="confirmed",
                rung="ssh+cdp",
                data={"already_running": True, "adopted": True,
                      "pid": adopted["pid"], "port": adopted["port"],
                      "profile": BROWSER_PROFILE,
                      "fingerprint": record["fingerprint"]},
            )
        # The UA a windowed run of THIS binary sends, read from the binary
        # before launch (`Browser.getVersion` needs a running browser and the
        # flag must ride on the launch itself). No readable version, no flag.
        probe = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                             ["sh", "-c", "chromium --version 2>&1; uname -m"],
                             timeout=8.0)
        probe_lines = [line.strip() for line in probe["stdout"].splitlines()
                       if line.strip()]
        user_agent = ua_from_version(
            probe_lines[0] if probe_lines else "",
            probe_lines[-1] if len(probe_lines) > 1 else "",
        )
        result = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                              ["sh", "-c", start_shell(requested, BROWSER_PROFILE,
                                                       BROWSER_LOG,
                                                       user_agent=user_agent)],
                              timeout=15.0)
        pid = _last_int(result["stdout"])
        if result["exit_code"] != 0 or not pid:
            raise MvError("browser-start-failed", "chromium did not launch",
                          detail={"stderr": result["stderr"][-300:],
                                  "stdout": result["stdout"][-200:]})
        # The port oracle differs by mode. With `--port 0` the profile's
        # `DevToolsActivePort` is the ONLY truth and chromium writes it; with an
        # explicit port chromium 152 does not write the file at all, so the
        # requested port is the port the process's own cmdline names, and the
        # file (when present) just confirms it. Never let this wait consume the
        # endpoint deadline.
        resolved = 0
        file_deadline = time.monotonic() + (START_TIMEOUT if not requested else 5.0)
        while time.monotonic() < file_deadline:
            raw = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                               ["cat", f"{BROWSER_PROFILE}/DevToolsActivePort"],
                               timeout=5.0)
            resolved = parse_active_port(raw["stdout"])
            if resolved:
                break
            time.sleep(0.3)
        if not resolved and requested:
            resolved = requested
        if not resolved:
            log = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                               ["tail", "-20", BROWSER_LOG], timeout=5.0)
            raise MvError("browser-start-failed",
                          "chromium never published a DevTools port "
                          "(required with --port 0)",
                          detail={"log": log["stdout"][-400:]})
        endpoint_deadline = time.monotonic() + START_TIMEOUT
        with sshx.forward(inst.key, inst.known_hosts, ip, resolved,
                          timeout=10.0) as local:
            info = _wait_version(local, endpoint_deadline)
        cmdline = _guest_cmdline(inst, ip, pid)
        reason = state.stale_reason(cmdline, BROWSER_PROFILE, resolved)
        if reason:
            raise MvError("browser-start-failed",
                          f"the launched pid does not look like our browser: {reason}")
        record = state.new_state(
            pid=pid, port=resolved, port_requested=requested,
            profile=BROWSER_PROFILE, log_path=BROWSER_LOG,
            fingerprint=state.fingerprint_of(info),
        )
        state.save(inst, record)
        return envelope(
            instance=inst.summary(meta), verified=True, effect="confirmed",
            rung="ssh+cdp",
            data={"pid": pid, "port": resolved, "port_requested": requested,
                  "profile": BROWSER_PROFILE, "log_path": BROWSER_LOG,
                  "fingerprint": record["fingerprint"],
                  "started": True, "already_running": False},
        )


def _wait_version(local: int, deadline: float) -> dict:
    last: MvError | None = None
    while time.monotonic() < deadline:
        try:
            return cdp.version(local, timeout=2.0)
        except MvError as exc:
            if exc.code != "cdp-unreachable":
                raise
            last = exc
            time.sleep(0.2)
    raise MvError("browser-start-failed",
                  "the DevTools endpoint never answered",
                  detail=last.message if last else None)


def stop(ns, home: Path) -> dict:
    inst, meta = _instance(ns, home)
    ip = meta["net"]["ip"]
    with inst.lock():
        record = state.load(inst)
        if record is None:
            return envelope(instance=inst.summary(meta), verified=True,
                            effect="confirmed", rung="fs",
                            data={"already_stopped": True})
        cmdline = _guest_cmdline(inst, ip, record["pid"])
        reason = state.stale_reason(cmdline, record["profile"], record["port"])
        if reason:
            state.clear(inst)
            return envelope(instance=inst.summary(meta), verified=True,
                            effect="confirmed", rung="fs",
                            data={"already_stopped": True, "stale": reason})
        pid = int(record["pid"])
        if getattr(ns, "dry_run", False):
            raise DryRun([f"kill {pid} in instance {inst.id}",
                          "wait for the process to be gone, clear browser.json"])
        script = (f"kill {pid} 2>/dev/null; "
                  "for i in 1 2 3 4 5 6 7 8 9 10; do "
                  f"kill -0 {pid} 2>/dev/null || exit 0; sleep 0.3; done; exit 1")
        result = sshx.run_cmd(inst.key, inst.known_hosts, ip,
                              ["sh", "-c", script], timeout=10.0)
        graceful = result["exit_code"] == 0
        if graceful:
            state.clear(inst)
        data = {"pid": pid, "graceful": graceful, "stopped": graceful}
        if not graceful:
            data["note"] = "the process did not exit on SIGTERM"
        return envelope(instance=inst.summary(meta), verified=graceful,
                        effect="confirmed" if graceful else "unconfirmed",
                        rung="ssh", data=data)


def restart(ns, home: Path) -> dict:
    stop(ns, home)
    return start(ns, home)


def status(ns, home: Path) -> dict:
    inst, meta = _instance(ns, home)
    ip = meta["net"]["ip"]
    record = _reconcile(inst, ip)
    if record is None:
        return envelope(instance=inst.summary(meta), verified=True,
                        effect="confirmed", rung="fs",
                        data={"running": False})
    data: dict = {
        "running": True, "pid": record["pid"], "port": record["port"],
        "port_requested": record.get("port_requested"),
        "profile": record["profile"], "log_path": record.get("log_path", ""),
        "started_at": record.get("started_at"),
        "uptime_s": _uptime_seconds(record.get("started_at", "")),
        "fingerprint": record.get("fingerprint"),
    }
    try:
        with sshx.forward(inst.key, inst.known_hosts, ip, record["port"],
                          timeout=10.0) as local:
            info = cdp.version(local, timeout=5.0)
            current = state.fingerprint_of(info)
            if state.fingerprint_matches(record.get("fingerprint") or {}, current):
                data["verified"] = True
                data["tabs"] = len(cdp.page_rows(local))
            else:
                data["verified"] = False
                data["reason"] = "the endpoint's fingerprint changed"
                data["changed"] = state.changed_fields(
                    record.get("fingerprint") or {}, current)
    except MvError as exc:
        data["verified"] = False
        data["reason"] = exc.message
        data["code"] = exc.code
    return envelope(instance=inst.summary(meta),
                    verified=bool(data.get("verified")),
                    effect="confirmed",
                    rung="ssh+cdp", data=data)


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #

def _act_open(ns, home: Path) -> dict:
    urls = [dom._check_url(url) for url in (getattr(ns, "urls", None) or [])]
    if not urls:
        raise MvError("bad-args", "open needs at least one URL")
    if getattr(ns, "dry_run", False):
        raise DryRun([f"open a tab for {url}" for url in urls])
    inst, meta = _instance(ns, home)
    timeout = _timeout_of(ns)
    with _session(inst, meta, timeout) as sess:
        rows = cdp.page_rows(sess.local)
        opened = []
        for index, url in enumerate(urls):
            # The startup tab is about:blank; `open URL` navigates IT rather
            # than leaving a blank sibling that makes every later unspecific
            # action `tab-ambiguous`. Any other tab means a real new tab.
            if (index == 0 and len(rows) == 1
                    and str(rows[0].get("url") or "") in ("", "about:blank")):
                sid = sess.cdp.attach(rows[0]["id"])
                sess.cdp.call("Page.navigate", {"url": url}, session_id=sid)
                opened.append({"id": rows[0]["id"], "url": url, "reused": True})
                continue
            result = sess.cdp.call("Target.createTarget", {"url": url})
            opened.append({"id": result.get("targetId"), "url": url,
                           "reused": False})
        rows = cdp.page_rows(sess.local)
        present = {row["id"] for row in rows}
        missing = [item["id"] for item in opened if item["id"] not in present]
        if missing:
            raise MvError("cdp-error", f"tabs were not created: {missing}")
        return _reply(sess, {"opened": opened, "count": len(opened)},
                      action="open")


def _act_tabs(ns, home: Path) -> dict:
    inst, meta = _instance(ns, home)
    with _session(inst, meta, _timeout_of(ns)) as sess:
        rows = cdp.page_rows(sess.local)
        tabs = []
        for row in rows:
            active = False
            try:
                sid = sess.cdp.attach(row["id"])
                active = bool(sess.cdp.evaluate(
                    "document.visibilityState === 'visible'", session_id=sid))
            except MvError:
                active = False
            tabs.append({"id": row["id"], "title": row.get("title", ""),
                         "url": row.get("url", ""), "active": active})
        return _reply(sess, {"tabs": tabs, "count": len(tabs)}, action="tabs")


def _act_close(ns, home: Path) -> dict:
    if getattr(ns, "dry_run", False):
        raise DryRun([f"close tabs {getattr(ns, 'specs', None) or '--all'}"])
    inst, meta = _instance(ns, home)
    with _session(inst, meta, _timeout_of(ns)) as sess:
        rows = cdp.page_rows(sess.local)
        if getattr(ns, "all", False):
            victims = list(rows)
        else:
            victims = []
            for spec in (getattr(ns, "specs", None) or []):
                tab = targets.resolve(rows, spec)
                if tab not in victims:
                    victims.append(tab)
        if not victims:
            raise MvError("bad-args", "close needs a SPEC or --all",
                          hint="close id:<prefix> [SPEC…], or close --all")
        for tab in victims:
            sess.cdp.call("Target.closeTarget", {"targetId": tab["id"]})
        # Closing is asynchronous; poll for absence, and treat the browser
        # exiting with its last tab as success — then there is no /json/list
        # left to ask.
        deadline = time.monotonic() + 3.0
        left: set[str] = set()
        while True:
            try:
                left = {row["id"] for row in cdp.page_rows(sess.local)}
            except MvError as exc:
                if exc.code == "cdp-unreachable":
                    left = set()
                    break
                raise
            if not any(tab["id"] in left for tab in victims):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        survivors = [tab["id"] for tab in victims if tab["id"] in left]
        if survivors:
            raise MvError("close-tab-not-verified",
                          f"{len(survivors)} tab(s) are still open",
                          detail={"survivors": survivors})
        return _reply(sess, {"closed": [tab["id"] for tab in victims],
                             "count": len(victims)}, action="close")


def _act_dom(ns, home: Path) -> dict:
    def run(sess: Session, sid: str, timeout: float) -> dict:
        if getattr(ns, "url", None):
            dom.nav(sess.cdp, sid, ns.url, timeout=min(timeout, 30.0))
        html = dom.dom_html(sess.cdp, sid, timeout)
        encoded = html.encode()
        data: dict = {"dom": html, "dom_bytes": len(encoded),
                      "dom_truncated": False, "out_path": None,
                      "dom_sha256": None, "url": getattr(ns, "url", None)}
        if len(encoded) > INLINE_LIMIT:
            if getattr(ns, "out", None):
                target = Path(ns.out).expanduser()
            else:
                target = sess.inst.dir / "artifacts" / f"dom-{int(time.time())}.html"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(html)
            except OSError as exc:
                raise MvError("write-failed", f"cannot write {target}",
                              detail=str(exc)) from exc
            data.update({"dom": None, "dom_truncated": True,
                         "out_path": str(target),
                         "dom_sha256": util.sha256_file(target)})
        return data

    return _action(ns, home, run, "dom")


def _act_screenshot(ns, home: Path) -> dict:
    def run(sess: Session, sid: str, timeout: float) -> dict:
        if getattr(ns, "url", None):
            dom.nav(sess.cdp, sid, ns.url, timeout=min(timeout, 30.0))
        out = getattr(ns, "out", None) or str(
            Path.cwd() / f"{ns.id}-browser-{int(time.time())}.png")
        return dom.screenshot(sess.cdp, sid, out,
                              full=bool(getattr(ns, "full", False)),
                              selector=getattr(ns, "selector", "") or "",
                              index=int(getattr(ns, "index", 0) or 0),
                              clip=getattr(ns, "clip", "") or "",
                              force=bool(getattr(ns, "force", False)),
                              timeout=timeout)

    return _action(ns, home, run, "screenshot")


def _act_pdf(ns, home: Path) -> dict:
    def run(sess: Session, sid: str, timeout: float) -> dict:
        if getattr(ns, "url", None):
            dom.nav(sess.cdp, sid, ns.url, timeout=min(timeout, 30.0))
        out = getattr(ns, "out", None) or str(
            Path.cwd() / f"{ns.id}-browser-{int(time.time())}.pdf")
        return dom.pdf(sess.cdp, sid, out,
                       force=bool(getattr(ns, "force", False)), timeout=timeout)

    return _action(ns, home, run, "pdf")


def _act_upload(ns, home: Path) -> dict:
    def run(sess: Session, sid: str, timeout: float) -> dict:
        guest_path = ns.guest_path
        check = sshx.run_cmd(sess.inst.key, sess.inst.known_hosts,
                             sess.meta["net"]["ip"],
                             ["test", "-f", guest_path], timeout=8.0)
        if check["exit_code"] != 0:
            raise MvError("no-file", f"{guest_path} does not exist in the guest",
                          hint="copy it first: microvm-control cp ./file "
                               f"{ns.id}:{guest_path}")
        return dom.upload(sess.cdp, sid, guest_path, ns.selector, timeout=timeout)

    return _action(ns, home, run, "upload")


def _act_nav(ns, home: Path) -> dict:
    if getattr(ns, "dry_run", False):
        raise DryRun([f"navigate the tab to {ns.url}"])
    return _action(ns, home,
                   lambda sess, sid, t: dom.nav(sess.cdp, sid, ns.url, timeout=t),
                   "nav")


def _act_history(ns, home: Path) -> dict:
    return _action(ns, home,
                   lambda sess, sid, t: dom.history(sess.cdp, sid, ns.browser_verb,
                                                    timeout=t),
                   ns.browser_verb)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

HANDLERS = {
    "start": start,
    "stop": stop,
    "restart": restart,
    "status": status,
    "open": _act_open,
    "tabs": _act_tabs,
    "close": _act_close,
    "nav": _act_nav,
    "back": _act_history,
    "forward": _act_history,
    "reload": lambda ns, home: _action(
        ns, home, lambda sess, sid, t: dom.reload(sess.cdp, sid, timeout=t), "reload"),
    "text": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.text(sess.cdp, sid, ns.selector or "",
                                      chars=ns.chars, timeout=t), "text"),
    "find": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.find(sess.cdp, sid, text=ns.text or "",
                                      selector=ns.selector or "", cap=ns.cap,
                                      timeout=t), "find"),
    "extract": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.extract(sess.cdp, sid, each=ns.each,
                                         fields=ns.field, cap=ns.cap,
                                         chars=ns.chars, visible=ns.visible,
                                         unique=ns.unique or "", timeout=t),
        "extract"),
    "dom": _act_dom,
    "click": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.click(sess.cdp, sid, text=ns.text or "",
                                       selector=ns.selector or "",
                                       index=ns.index or 0, at=ns.at or "",
                                       timeout=t), "click"),
    "hover": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.hover(sess.cdp, sid, text=ns.text or "",
                                       selector=ns.selector or "",
                                       index=ns.index or 0, timeout=t), "hover"),
    "focus": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.focus(sess.cdp, sid, text=ns.text or "",
                                       selector=ns.selector or "",
                                       index=ns.index or 0, timeout=t), "focus"),
    "press": lambda ns, home: _action(
        ns, home, lambda sess, sid, t: dom.press(sess.cdp, sid, ns.key, timeout=t),
        "press"),
    "insert": lambda ns, home: _action(
        ns, home, lambda sess, sid, t: dom.insert(sess.cdp, sid, ns.value, timeout=t),
        "insert"),
    "type": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.type_text(sess.cdp, sid, ns.value, timeout=t,
                                           delay=getattr(ns, "delay", 0.0) or 0.0),
        "type"),
    "check": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.check(sess.cdp, sid, text=ns.text or "",
                                       selector=ns.selector or "",
                                       index=ns.index or 0, uncheck=ns.uncheck,
                                       timeout=t), "check"),
    "select": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.select(sess.cdp, sid, text=ns.text or "",
                                        selector=ns.selector or "",
                                        index=ns.index or 0, value=ns.value,
                                        timeout=t), "select"),
    "scroll": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.scroll(sess.cdp, sid, by=ns.by,
                                        edge=ns.edge or "", timeout=t), "scroll"),
    "wait": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.wait(sess.cdp, sid, ns.for_,
                                      selector=ns.selector or "",
                                      expr=ns.expr or "", timeout=t,
                                      idle_ms=ns.idle_ms), "wait"),
    "js": lambda ns, home: _action(
        ns, home, lambda sess, sid, t: dom.js(sess.cdp, sid, ns.expr, timeout=t),
        "js"),
    "dialog": lambda ns, home: _action(
        ns, home,
        lambda sess, sid, t: dom.dialog(sess.cdp, sid, ns.mode,
                                        text=getattr(ns, "text", "") or "",
                                        timeout=t), "dialog"),
    "screenshot": _act_screenshot,
    "pdf": _act_pdf,
    "upload": _act_upload,
}


def dispatch(ns, home: Path) -> dict:
    verb = getattr(ns, "browser_verb", None)
    if verb is None:
        from .. import lifecycle
        return lifecycle.browser(ns, home)
    handler = HANDLERS.get(verb)
    if handler is None:
        raise MvError("bad-args", f"unknown browser action {verb!r}")
    return handler(ns, home)
