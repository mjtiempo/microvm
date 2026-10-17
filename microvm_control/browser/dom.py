"""dom — the page verbs: read, act, and prove it happened.

The vendored core from `mark-browser-control` (MIT), rewritten as direct
functions over `Cdp` with the same discipline: a mutation is never `ok: true`
on a bare acknowledgement, `verified: false` is honest where no oracle can
know (`js`, `press`, a `--at` click), and every refusal names its code.

Conventions:

* every function takes `(cdp, sid, ...)` — one CDP connection and the FLAT
  session attached to the tab — and returns the `data` dict of the reply;
* refusals are `MvError` with the codes in `references/errors.md`;
* expressions are built with `json.dumps` for values, so page text can never
  change the shape of the script.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import time
from pathlib import Path

from ..errors import MvError

__all__ = [
    "check", "click", "dialog", "dom_html", "extract", "find", "history",
    "hover", "insert", "js", "nav", "pdf", "press", "reload", "screenshot",
    "scroll", "select", "snapshot", "text", "type_text", "upload", "wait",
]

DEFAULT_TEXT_CHARS = 4000
MAX_TEXT_CHARS = 200_000
DEFAULT_EXTRACT_CHARS = 1000
MAX_RESULT = 100_000
CLICK_SETTLE_S = 2.0
POLL_S = 0.1

SNAPSHOT_JS = ("({url: location.href, title: document.title, "
               "scroll: Math.round(window.scrollY)})")
READY_JS = "({url: location.href, ready: document.readyState})"
ACTIVE_LENGTH_JS = (
    "(() => { const el = document.activeElement;"
    " const editable = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'"
    " || el.isContentEditable);"
    " if (!editable) return -1;"
    " return String(el.value !== undefined ? el.value : (el.innerText || '')).length;"
    "})()"
)
_CHECKED_JS = (
    "(() => { const el = %s;"
    " if (!el) return null;"
    " if (typeof el.checked === 'boolean') return el.checked;"
    " const aria = el.getAttribute && el.getAttribute('aria-checked');"
    " if (aria === null) return null;"
    " return aria === 'true';"
    "})()"
)


def snapshot(cdp, sid: str, timeout: float | None = None) -> dict:
    """The page's own url/title/scroll — the before/after oracle."""
    value = cdp.evaluate(SNAPSHOT_JS, session_id=sid, timeout=timeout)
    if not isinstance(value, dict):
        raise MvError("cdp-error", "the page did not answer with a snapshot")
    return value


# --------------------------------------------------------------------------- #
# element resolution
# --------------------------------------------------------------------------- #

def _resolve_js(text: str, selector: str, index: int, do_focus: bool = False) -> str:
    return (
        "(() => {"
        f" const wanted = {json.dumps(text or '')};"
        f" const sel = {json.dumps(selector or '')};"
        f" const idx = {int(index)};"
        " let all = [];"
        " if (sel) { all = [...document.querySelectorAll(sel)]; }"
        " else {"
        "   const pool = [...document.querySelectorAll("
        "     'a,button,input,select,textarea,summary,label,[role],[onclick],[tabindex]')];"
        "   const t = wanted.trim().toLowerCase();"
        "   const exact = pool.filter(el =>"
        "     ((el.innerText || el.value || el.getAttribute('aria-label') || '')"
        "       .trim().toLowerCase() === t));"
        "   const loose = pool.filter(el =>"
        "     ((el.innerText || el.value || el.getAttribute('aria-label') || '')"
        "       .trim().toLowerCase().includes(t)));"
        "   all = exact.length ? exact : loose;"
        " }"
        " const visible = el => { const s = getComputedStyle(el);"
        "   const r = el.getBoundingClientRect();"
        "   return s.display !== 'none' && s.visibility !== 'hidden'"
        "     && r.width > 0 && r.height > 0; };"
        " const els = all.filter(visible);"
        " if (!els.length) return {found: false, total: all.length};"
        " if (idx < 0 || idx >= els.length) return {found: false, total: els.length, index: idx};"
        " const el = els[idx];"
        " el.scrollIntoView({block: 'center', inline: 'center'});"
        + (" el.focus();" if do_focus else "") +
        " const r = el.getBoundingClientRect();"
        " const cx = r.left + r.width / 2, cy = r.top + r.height / 2;"
        " const hit = document.elementFromPoint(cx, cy);"
        " return {found: true, total: els.length, tag: el.tagName.toLowerCase(),"
        "   text: String(el.innerText || el.value || '').trim().slice(0, 200),"
        "   box: {x: r.left, y: r.top, w: r.width, h: r.height},"
        "   center: {x: cx, y: cy}, in_viewport:"
        "     cy >= 0 && cy <= innerHeight && cx >= 0 && cx <= innerWidth,"
        "   hit: hit === el || el.contains(hit) || (hit !== null && hit.contains(el)),"
        "   focused: document.activeElement === el,"
        "   disabled: !!el.disabled};"
        "})()"
    )


def _resolve(cdp, sid: str, *, text: str = "", selector: str = "",
             index: int = 0, timeout: float | None = None,
             do_focus: bool = False) -> dict:
    if not text and not selector:
        raise MvError("bad-args", "give TEXT or --selector to name the element")
    found = cdp.evaluate(_resolve_js(text, selector, index, do_focus),
                         session_id=sid, timeout=timeout)
    if not isinstance(found, dict):
        raise MvError("cdp-error", "the page did not answer with a match")
    if not found.get("found"):
        total = int(found.get("total") or 0)
        raise MvError(
            "no-match",
            f"nothing matched {selector or text!r} (index {index})",
            hint="check with find --selector/text, or raise --index",
            detail={"total": total, "index": index},
        )
    return found


def _dispatch_mouse(cdp, sid: str, kind: str, x: float, y: float,
                    button: str = "left") -> None:
    cdp.call("Input.dispatchMouseEvent",
             {"type": kind, "x": x, "y": y, "button": button,
              "buttons": 1 if kind == "mousePressed" else 0,
              "clickCount": 1 if kind == "mousePressed" else 0},
             session_id=sid)


def _click_at(cdp, sid: str, x: float, y: float) -> None:
    _dispatch_mouse(cdp, sid, "mousePressed", x, y)
    _dispatch_mouse(cdp, sid, "mouseReleased", x, y)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def text(cdp, sid: str, selector: str = "", chars: int = DEFAULT_TEXT_CHARS,
         timeout: float | None = None) -> dict:
    chars = max(0, min(int(chars), MAX_TEXT_CHARS))
    expression = (
        "(() => {"
        f" const el = {json.dumps(selector or '')}"
        "   ? document.querySelector(" + json.dumps(selector or "") + ")"
        "   : document.body;"
        " if (!el) return {text: '', length: 0};"
        " const t = String(el.innerText || el.textContent || '');"
        f" return {{text: t.slice(0, {chars}), length: t.length}};"
        "})()"
    )
    value = cdp.evaluate(expression, session_id=sid, timeout=timeout)
    if not isinstance(value, dict):
        raise MvError("cdp-error", "the page did not answer with text")
    body = str(value.get("text") or "")
    length = int(value.get("length") or len(body))
    return {"text": body, "length": length, "truncated": length > len(body),
            "selector": selector or ""}


def find(cdp, sid: str, text: str = "", selector: str = "", cap: int = 10,
         timeout: float | None = None) -> dict:
    cap = max(1, min(int(cap), 500))
    expression = (
        "(() => {"
        f" const wanted = {json.dumps(text or '')};"
        f" const sel = {json.dumps(selector or '')};"
        f" const cap = {cap};"
        " let all = sel ? [...document.querySelectorAll(sel)] :"
        "   [...document.querySelectorAll('a,button,input,select,textarea,summary,"
        "label,[role],[onclick],[tabindex]')];"
        " if (wanted) { const t = wanted.trim().toLowerCase();"
        "   all = all.filter(el => String(el.innerText || el.value || '')"
        "     .trim().toLowerCase().includes(t)); }"
        " const visible = el => { const s = getComputedStyle(el);"
        "   const r = el.getBoundingClientRect();"
        "   return s.display !== 'none' && s.visibility !== 'hidden'"
        "     && r.width > 0 && r.height > 0; };"
        " const els = all.filter(visible);"
        " const rows = els.slice(0, cap).map(el => {"
        "   const r = el.getBoundingClientRect();"
        "   const cx = r.left + r.width / 2, cy = r.top + r.height / 2;"
        "   const hit = document.elementFromPoint(cx, cy);"
        "   return {tag: el.tagName.toLowerCase(),"
        "     text: String(el.innerText || el.value || '').trim().slice(0, 200),"
        "     href: el.getAttribute ? (el.getAttribute('href') || '') : '',"
        "     box: {x: r.left, y: r.top, w: r.width, h: r.height},"
        "     center: {x: cx, y: cy},"
        "     in_viewport: cy >= 0 && cy <= innerHeight && cx >= 0 && cx <= innerWidth,"
        "     hit: hit === el || el.contains(hit) || (hit !== null && hit.contains(el))};"
        " });"
        " return {matches: rows, total: els.length};"
        "})()"
    )
    value = cdp.evaluate(expression, session_id=sid, timeout=timeout)
    if not isinstance(value, dict):
        raise MvError("cdp-error", "the page did not answer with matches")
    matches = value.get("matches") or []
    return {"matches": matches, "count": len(matches),
            "total": int(value.get("total") or len(matches)),
            "selector": selector or "", "text": text or ""}


def _field_specs(fields: list[str]) -> list[dict]:
    specs = []
    for raw in fields:
        name, _, target = str(raw).partition("=")
        if not name or not target:
            raise MvError("bad-args", f"field {raw!r} must be NAME=SELECTOR[@attr]")
        selector, _, attr = target.partition("@")
        specs.append({"name": name,
                      "selector": selector or ":scope",
                      "attr": attr or ""})
    return specs


def extract(cdp, sid: str, each: str, fields: list[str], cap: int = 10,
            chars: int = DEFAULT_EXTRACT_CHARS, visible: bool = False,
            unique: str = "", timeout: float | None = None) -> dict:
    if not each:
        raise MvError("bad-args", "extract needs --each SELECTOR")
    specs = _field_specs(fields)
    if not specs:
        raise MvError("bad-args", "extract needs at least one --field NAME=SELECTOR")
    cap = max(1, min(int(cap), 500))
    chars = max(1, min(int(chars), 20_000))
    expression = (
        "(() => {"
        f" const specs = {json.dumps(specs)};"
        f" const cap = {cap}; const chars = {chars}; const visibleOnly = {str(bool(visible)).lower()};"
        f" const unique = {json.dumps(unique or '')};"
        " const visible = el => { const s = getComputedStyle(el);"
        "   const r = el.getBoundingClientRect();"
        "   return s.display !== 'none' && s.visibility !== 'hidden'"
        "     && r.width > 0 && r.height > 0; };"
        f" let items = [...document.querySelectorAll({json.dumps(each)})];"
        " if (visibleOnly) items = items.filter(visible);"
        " const readOne = (item, spec) => {"
        "   let el = item;"
        "   if (spec.selector !== ':scope') el = item.querySelector(spec.selector);"
        "   if (!el) return null;"
        "   const value = spec.attr ? el.getAttribute(spec.attr)"
        "     : String(el.innerText || el.textContent || '').trim();"
        "   return value === null || value === undefined ? null : String(value).slice(0, chars);"
        " };"
        " const rows = []; const seen = new Set();"
        " for (const item of items) {"
        "   const row = {};"
        "   for (const spec of specs) row[spec.name] = readOne(item, spec);"
        "   if (unique) { const key = row[unique];"
        "     if (key !== undefined && key !== null) {"
        "       if (seen.has(String(key))) continue; seen.add(String(key)); } }"
        "   rows.push(row);"
        " }"
        " return {records: rows.slice(0, cap), total: rows.length,"
        "   truncated: rows.length > cap};"
        "})()"
    )
    value = cdp.evaluate(expression, session_id=sid, timeout=timeout)
    if not isinstance(value, dict):
        raise MvError("cdp-error", "the page did not answer with records")
    records = value.get("records") or []
    return {"each": each, "matches": records, "count": len(records),
            "total": int(value.get("total") or len(records)),
            "truncated": bool(value.get("truncated"))}


def dom_html(cdp, sid: str, timeout: float | None = None) -> str:
    value = cdp.evaluate("document.documentElement.outerHTML",
                         session_id=sid, timeout=timeout)
    if value is None:
        raise MvError("cdp-error", "the page did not answer with a DOM")
    return str(value)


# --------------------------------------------------------------------------- #
# interactions
# --------------------------------------------------------------------------- #

def click(cdp, sid: str, text: str = "", selector: str = "", index: int = 0,
          at: str = "", timeout: float | None = None) -> dict:
    if at:
        try:
            x_raw, y_raw = str(at).split(",", 1)
            x, y = float(x_raw), float(y_raw)
        except ValueError as exc:
            raise MvError("bad-args", f"--at expects X,Y (got {at!r})") from exc
        before = snapshot(cdp, sid, timeout)
        under = cdp.evaluate(
            "(() => { const el = document.elementFromPoint(%f, %f);"
            " if (!el) return null;"
            " return {tag: el.tagName.toLowerCase(),"
            "  text: String(el.innerText || '').trim().slice(0, 200),"
            "  summary: el.tagName.toLowerCase()}; })()" % (x, y),
            session_id=sid, timeout=timeout)
        _click_at(cdp, sid, x, y)
        after, changed = _settle(cdp, sid, before, timeout)
        return {"clicked": True, "changed": changed, "before": before,
                "after": after, "under": under or {}, "verified": False,
                "note": "a point click reports what it reached; read `changed` yourself"}
    found = _resolve(cdp, sid, text=text, selector=selector, index=index,
                     timeout=timeout)
    if not found.get("hit"):
        center = found.get("center") or {}
        raise MvError(
            "occluded",
            f"the centre of {found.get('tag')} {found.get('text')!r} is covered",
            hint="scroll it into view, use --at, or pick another target",
            detail={"element": found.get("tag"), "center": center},
        )
    center = found.get("center") or {}
    before = snapshot(cdp, sid, timeout)
    _click_at(cdp, sid, float(center.get("x") or 0), float(center.get("y") or 0))
    after, changed = _settle(cdp, sid, before, timeout)
    return {"clicked": True, "changed": changed, "before": before, "after": after,
            "element": {"tag": found.get("tag"), "text": found.get("text"),
                        "box": found.get("box"), "center": center},
            "verified": True}


def _settle(cdp, sid: str, before: dict, timeout: float | None,
            budget: float = CLICK_SETTLE_S) -> tuple[dict, bool]:
    """Read the page back until it moves, or the settle budget runs out."""
    deadline = time.monotonic() + budget
    after = snapshot(cdp, sid, timeout)
    while after == before and time.monotonic() < deadline:
        time.sleep(POLL_S)
        after = snapshot(cdp, sid, timeout)
    return after, after != before


def hover(cdp, sid: str, text: str = "", selector: str = "", index: int = 0,
          timeout: float | None = None) -> dict:
    found = _resolve(cdp, sid, text=text, selector=selector, index=index,
                     timeout=timeout)
    center = found.get("center") or {}
    cdp.call("Input.dispatchMouseEvent",
             {"type": "mouseMoved", "x": center.get("x"), "y": center.get("y")},
             session_id=sid)
    hovered = cdp.evaluate(
        "(() => { const el = document.elementFromPoint(%f, %f);"
        " return !!el && el.matches(':hover'); })()"
        % (float(center.get("x") or 0), float(center.get("y") or 0)),
        session_id=sid, timeout=timeout)
    return {"hovered": bool(hovered), "verified": bool(hovered),
            "element": {"tag": found.get("tag"), "text": found.get("text")}}


def focus(cdp, sid: str, text: str = "", selector: str = "", index: int = 0,
          timeout: float | None = None) -> dict:
    found = _resolve(cdp, sid, text=text, selector=selector, index=index,
                     timeout=timeout, do_focus=True)
    if not found.get("focused"):
        raise MvError("focus-not-verified",
                      f"focus did not land on {found.get('tag')} "
                      f"{found.get('text')!r}")
    return {"focused": True,
            "element": {"tag": found.get("tag"), "text": found.get("text")}}


def press(cdp, sid: str, key: str, timeout: float | None = None) -> dict:
    spec = _key_spec(key)
    cdp.call("Input.dispatchKeyEvent", {**spec, "type": "keyDown"}, session_id=sid)
    cdp.call("Input.dispatchKeyEvent", {**spec, "type": "keyUp"}, session_id=sid)
    return {"key": key, "dispatched": True, "verified": False,
            "note": "the protocol only acknowledges the dispatch; read the effect yourself"}


KEYS = {
    "enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
    "tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "esc": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46},
    "space": {"key": " ", "code": "Space", "text": " ",
              "windowsVirtualKeyCode": 32},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "home": {"key": "Home", "code": "Home", "windowsVirtualKeyCode": 36},
    "end": {"key": "End", "code": "End", "windowsVirtualKeyCode": 35},
}


def _key_spec(key: str) -> dict:
    wanted = str(key or "")
    spec = KEYS.get(wanted.lower())
    if spec is not None:
        return dict(spec)
    if len(wanted) == 1:
        return {"key": wanted, "code": f"Key{wanted.upper()}", "text": wanted}
    raise MvError("bad-args", f"unknown key {key!r}",
                  hint="enter, tab, escape, backspace, arrows, home, end, or one character")


def insert(cdp, sid: str, value: str, timeout: float | None = None) -> dict:
    before = cdp.evaluate(ACTIVE_LENGTH_JS, session_id=sid, timeout=timeout)
    if not isinstance(before, int) or before < 0:
        raise MvError("no-focus", "nothing editable is focused",
                      hint="focus a field first: browser <id> focus --selector CSS")
    cdp.call("Input.insertText", {"text": value}, session_id=sid)
    after = cdp.evaluate(ACTIVE_LENGTH_JS, session_id=sid, timeout=timeout)
    after = int(after) if isinstance(after, int) else before
    verified = after == before + len(value)
    return {"inserted": len(value), "length_before": before,
            "length_after": after, "verified": verified,
            **({} if verified else {"note": "the field did not grow by the inserted length"})}


def type_text(cdp, sid: str, value: str, timeout: float | None = None,
              delay: float = 0.0, _sleep=time.sleep) -> dict:
    """Per-character key events; `delay` paces the keystrokes (0..5 s)."""
    pace = float(delay or 0.0)
    if pace < 0 or pace > 5:
        raise MvError("bad-args", f"--delay must be 0..5 seconds (got {pace})")
    before = cdp.evaluate(ACTIVE_LENGTH_JS, session_id=sid, timeout=timeout)
    if not isinstance(before, int) or before < 0:
        raise MvError("no-focus", "nothing editable is focused",
                      hint="focus a field first: browser <id> focus --selector CSS")
    for index, char in enumerate(value):
        if pace and index:
            _sleep(pace)
        spec = KEYS.get("enter") if char == "\n" else None
        if spec is None:
            spec = {"key": char, "code": f"Key{char.upper()}", "text": char}
        cdp.call("Input.dispatchKeyEvent", {**spec, "type": "keyDown"}, session_id=sid)
        cdp.call("Input.dispatchKeyEvent", {**spec, "type": "keyUp"}, session_id=sid)
    after = cdp.evaluate(ACTIVE_LENGTH_JS, session_id=sid, timeout=timeout)
    after = int(after) if isinstance(after, int) else before
    verified = after == before + len(value)
    return {"typed": len(value), "length_before": before, "length_after": after,
            "delay_s": pace, "verified": verified,
            **({} if verified else {"note": "the field did not grow by the typed length"})}


def check(cdp, sid: str, text: str = "", selector: str = "", index: int = 0,
          uncheck: bool = False, timeout: float | None = None) -> dict:
    found = _resolve(cdp, sid, text=text, selector=selector, index=index,
                     timeout=timeout)
    want = not uncheck
    target = json.dumps(selector or text or "")
    state = cdp.evaluate(
        _CHECKED_JS % (f"document.querySelector({target})" if selector
                       else "document.activeElement"),
        session_id=sid, timeout=timeout)
    if state is None:
        raise MvError("not-checkable",
                      f"{found.get('tag')} {found.get('text')!r} has no checked state")
    if bool(state) == want:
        return {"checked": bool(state), "checked_before": bool(state),
                "changed": False, "verified": True}
    center = found.get("center") or {}
    _click_at(cdp, sid, float(center.get("x") or 0), float(center.get("y") or 0))
    after = cdp.evaluate(
        _CHECKED_JS % (f"document.querySelector({target})" if selector
                       else "document.activeElement"),
        session_id=sid, timeout=timeout)
    after = bool(after) if after is not None else not bool(state)
    if after != want:
        raise MvError("check-not-verified",
                      f"the control is still {'checked' if after else 'unchecked'}")
    return {"checked": after, "checked_before": bool(state), "changed": True,
            "verified": True}


def select(cdp, sid: str, text: str = "", selector: str = "", index: int = 0,
           value: str = "", timeout: float | None = None) -> dict:
    found = _resolve(cdp, sid, text=text, selector=selector, index=index,
                     timeout=timeout)
    if not selector:
        raise MvError("bad-args", "select needs --selector to name the <select>")
    result = cdp.evaluate(
        "(() => { const el = document.querySelector(%s);"
        " if (!el || el.tagName !== 'SELECT') return null;"
        " const wanted = %s;"
        " const opts = [...el.options];"
        " let by = 'value'; let matches = opts.filter(o => o.value === wanted);"
        " if (!matches.length) { by = 'label';"
        "   matches = opts.filter(o => o.text.trim() === wanted); }"
        " if (!matches.length) return {selected: false,"
        "   options: opts.map(o => o.text.trim()).slice(0, 20)};"
        " if (matches.length > 1) return {selected: false, ambiguous: matches.length,"
        "   options: matches.map(o => o.text.trim())};"
        " el.value = matches[0].value;"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true}));"
        " return {selected: true, value: el.value, by,"
        "   label: matches[0].text.trim()}; })()"
        % (json.dumps(selector), json.dumps(value)),
        session_id=sid, timeout=timeout)
    if not isinstance(result, dict):
        raise MvError("not-a-select", f"{selector!r} is not a <select>")
    if not result.get("selected"):
        options = result.get("options") or []
        if result.get("ambiguous"):
            raise MvError("ambiguous-option",
                          f"{result['ambiguous']} options match {value!r}",
                          hint="use the exact option value",
                          detail={"options": options})
        raise MvError("no-match", f"no option matches {value!r}",
                      hint="the options are in error.detail",
                      detail={"options": options})
    return {"selected": True, "value": result.get("value"), "by": result.get("by"),
            "label": result.get("label"), "verified": True}


def scroll(cdp, sid: str, by: int | None = None, edge: str = "",
           timeout: float | None = None) -> dict:
    if by is None and edge not in ("top", "bottom"):
        raise MvError("bad-args", "scroll needs --by N or --edge top|bottom")
    before = cdp.evaluate("Math.round(window.scrollY)", session_id=sid, timeout=timeout)
    before = int(before or 0)
    delta = int(by) if by is not None else (-100_000 if edge == "top" else 100_000)
    cdp.call("Input.dispatchMouseEvent",
             {"type": "mouseWheel", "x": 200, "y": 200, "deltaX": 0, "deltaY": delta},
             session_id=sid)
    deadline = time.monotonic() + 1.5
    after = before
    while time.monotonic() < deadline:
        after = cdp.evaluate("Math.round(window.scrollY)", session_id=sid, timeout=timeout)
        after = int(after or 0)
        if after != before:
            break
        time.sleep(POLL_S)
    return {"moved": after != before, "before": before, "after": after,
            "verified": after != before,
            **({} if after != before else {"note": "the page did not move"})}


# --------------------------------------------------------------------------- #
# waiting
# --------------------------------------------------------------------------- #

def wait(cdp, sid: str, for_: str, selector: str = "", expr: str = "",
         timeout: float = 30.0, idle_ms: int = 500,
         poll_interval: float = 0.25, _sleep=time.sleep) -> dict:
    kind = str(for_ or "")
    if kind == "element" and not selector:
        raise MvError("bad-args", "--for element needs --selector")
    if kind == "js" and not expr:
        raise MvError("bad-args", "--for js needs --expr")
    if kind not in ("load", "idle", "element", "js"):
        raise MvError("bad-args", f"unknown --for {for_!r}",
                      hint="load, idle, element, js")
    if kind == "element":
        predicate = f"(() => !!document.querySelector({json.dumps(selector)}))()"
        wanted = f"element {selector!r}"
    elif kind == "js":
        predicate = f"(() => !!({expr}))()"
        wanted = f"js {expr!r}"
    else:
        predicate = "document.readyState === 'complete'"
        wanted = kind
    started = time.monotonic()
    deadline = started + max(0.01, float(timeout))
    while True:
        ok = bool(cdp.evaluate(predicate, session_id=sid, timeout=timeout))
        if ok and kind == "idle":
            _sleep(max(0, idle_ms) / 1000.0)
            ok = bool(cdp.evaluate(predicate, session_id=sid, timeout=timeout))
        if ok:
            return {"for": kind, "waited_s": round(time.monotonic() - started, 3)}
        if time.monotonic() >= deadline:
            raise MvError(
                "wait-timeout",
                f"waited {timeout:.1f}s for {wanted} and it never happened",
                hint="raise --timeout or verify the page state",
            )
        _sleep(poll_interval)


# --------------------------------------------------------------------------- #
# navigation
# --------------------------------------------------------------------------- #

def _check_url(url: str) -> str:
    cleaned = str(url or "").strip()
    if cleaned in ("about:blank",):
        return cleaned
    if not cleaned.lower().startswith(("http://", "https://")):
        raise MvError("bad-args", f"{url!r} is not an http(s) URL",
                      hint="only http(s) and about:blank are allowed")
    return cleaned


def _normal(url: str) -> str:
    cleaned = str(url or "")
    return cleaned[:-1] if cleaned.endswith("/") else cleaned


def _read_ready(cdp, sid: str, timeout: float | None) -> dict:
    value = cdp.evaluate(READY_JS, session_id=sid, timeout=timeout)
    return value if isinstance(value, dict) else {}


def nav(cdp, sid: str, url: str, timeout: float = 30.0,
        poll_interval: float = 0.2, _sleep=time.sleep) -> dict:
    target = _check_url(url)
    cdp.call("Page.navigate", {"url": target}, session_id=sid)
    deadline = time.monotonic() + max(0.05, float(timeout))
    last: dict = {}
    while True:
        last = _read_ready(cdp, sid, timeout)
        observed = str(last.get("url") or "")
        if observed.startswith("chrome-error://"):
            raise MvError("nav-failed", f"{target} did not load (error page)",
                          detail={"url": observed})
        if _normal(observed) == _normal(target) and last.get("ready") == "complete":
            return {"url": target, "url_read": observed, "moved": True,
                    "ready": last.get("ready"), "verified": True}
        if time.monotonic() >= deadline:
            raise MvError(
                "nav-not-verified",
                f"navigation to {target} was not observed (still at {observed or '?'})",
                detail={"requested": target, "observed": observed},
            )
        _sleep(poll_interval)


def reload(cdp, sid: str, timeout: float = 10.0, poll_interval: float = 0.1,
           _sleep=time.sleep) -> dict:
    before = cdp.evaluate("performance.timeOrigin", session_id=sid, timeout=timeout)
    cdp.call("Page.reload", {}, session_id=sid)
    deadline = time.monotonic() + max(0.05, float(timeout))
    after = before
    while time.monotonic() < deadline:
        after = cdp.evaluate("performance.timeOrigin", session_id=sid, timeout=timeout)
        if after != before:
            return {"verified": True, "before": before, "after": after}
        _sleep(poll_interval)
    raise MvError("reload-not-verified", "the document did not restart")


def history(cdp, sid: str, direction: str, timeout: float = 10.0,
            poll_interval: float = 0.1, _sleep=time.sleep) -> dict:
    if direction not in ("back", "forward"):
        raise MvError("bad-args", f"unknown history direction {direction!r}")
    before = _read_ready(cdp, sid, timeout)
    start_url = str(before.get("url") or "")
    cdp.call("Runtime.evaluate", {"expression": f"history.{direction}()",
                                  "returnByValue": True}, session_id=sid)
    deadline = time.monotonic() + max(0.05, float(timeout))
    while time.monotonic() < deadline:
        current = _read_ready(cdp, sid, timeout)
        if str(current.get("url") or "") != start_url:
            return {"moved": True, "url": current.get("url"), "before": start_url,
                    "verified": True}
        _sleep(poll_interval)
    return {"moved": False, "url": start_url, "before": start_url,
            "verified": False,
            "note": f"history.{direction} did not change the URL"}


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #

def _png_size(raw: bytes) -> tuple[int, int]:
    if len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise MvError("screenshot-not-verified", "the answer is not a PNG")
    width, height = struct.unpack(">II", raw[16:24])
    return int(width), int(height)


def _write_out(out: Path, payload: bytes) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
    except OSError as exc:
        raise MvError("write-failed", f"cannot write {out}", detail=str(exc)) from exc


def _check_out(out: Path, force: bool) -> None:
    if out.exists() and not force:
        raise MvError("file-exists", f"{out} already exists",
                      hint="pass --force to overwrite")


def _parse_clip(clip: str) -> dict:
    try:
        x_raw, y_raw, w_raw, h_raw = str(clip).split(",")
        x, y, w, h = float(x_raw), float(y_raw), float(w_raw), float(h_raw)
    except ValueError as exc:
        raise MvError("bad-args", f"--clip expects X,Y,W,H (got {clip!r})") from exc
    if w <= 0 or h <= 0:
        raise MvError("bad-args", f"--clip needs a positive size (got {clip!r})")
    return {"x": x, "y": y, "width": w, "height": h}


def screenshot(cdp, sid: str, out: str, full: bool = False, selector: str = "",
               index: int = 0, clip: str = "", force: bool = False,
               timeout: float | None = None) -> dict:
    modes = [name for name, on in (("full", full), ("element", bool(selector)),
                                   ("clip", bool(clip))) if on]
    if len(modes) > 1:
        raise MvError("bad-args", "--full, --selector and --clip are one choice")
    target = Path(out).expanduser()
    _check_out(target, force)
    mode = modes[0] if modes else "viewport"
    if mode == "clip":
        region = _parse_clip(clip)
    else:
        region = None
    metrics = cdp.evaluate(
        "({dpr: window.devicePixelRatio || 1, iw: innerWidth, ih: innerHeight,"
        " sw: document.documentElement.scrollWidth,"
        " sh: document.documentElement.scrollHeight})",
        session_id=sid, timeout=timeout)
    if not isinstance(metrics, dict):
        raise MvError("no-viewport", "the page reports no viewport")
    dpr = float(metrics.get("dpr") or 1)
    if mode == "element":
        rect = cdp.evaluate(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return null; const r = el.getBoundingClientRect();"
            " return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height};"
            "})()" % json.dumps(selector),
            session_id=sid, timeout=timeout)
        if not isinstance(rect, dict):
            raise MvError("no-match", f"no element matches {selector!r}")
        left = math.floor(float(rect.get("x") or 0))
        top = math.floor(float(rect.get("y") or 0))
        right = math.ceil(float(rect.get("x") or 0) + float(rect.get("w") or 0))
        bottom = math.ceil(float(rect.get("y") or 0) + float(rect.get("h") or 0))
        region = {"x": float(left), "y": float(top),
                  "width": float(right - left), "height": float(bottom - top)}
    elif mode == "full":
        region = {"x": 0.0, "y": 0.0, "width": float(metrics.get("sw") or 0),
                  "height": float(metrics.get("sh") or 0)}
    elif mode == "viewport":
        region = None
    if mode == "viewport":
        expected = (float(metrics.get("iw") or 0), float(metrics.get("ih") or 0))
    elif region is not None:
        expected = (region["width"], region["height"])
    else:
        raise MvError("internal", f"screenshot mode {mode!r} resolved no region")
    params: dict = {"format": "png"}
    if region is not None:
        params["clip"] = {**region, "scale": 1}
        params["captureBeyondViewport"] = True
    result = cdp.call("Page.captureScreenshot", params, session_id=sid,
                      timeout=timeout)
    payload = result.get("data")
    if not payload:
        raise MvError("screenshot-not-verified", "the browser produced no PNG")
    try:
        raw = base64.b64decode(payload)
    except (ValueError, TypeError) as exc:
        raise MvError("screenshot-not-verified", f"the PNG is not base64: {exc}") from exc
    width, height = _png_size(raw)
    want_w = math.floor(expected[0] * dpr + 0.5)
    want_h = math.floor(expected[1] * dpr + 0.5)
    if (want_w, want_h) != (width, height):
        raise MvError(
            "screenshot-not-verified",
            f"the PNG is {width}x{height}; the page's {expected[0]:g}x{expected[1]:g} "
            f"at dpr {dpr:g} should be {want_w}x{want_h}",
            detail={"png": [width, height], "expected": [want_w, want_h]},
        )
    _write_out(target, raw)
    return {"path": str(target), "bytes": len(raw), "width": width, "height": height,
            "mode": mode, "clip": ([region["x"], region["y"], region["width"],
                                    region["height"]] if region else None),
            "sha256": hashlib.sha256(raw).hexdigest(), "verified": True}


def pdf(cdp, sid: str, out: str, force: bool = False,
        timeout: float | None = None) -> dict:
    target = Path(out).expanduser()
    _check_out(target, force)
    result = cdp.call("Page.printToPDF", {}, session_id=sid, timeout=timeout)
    payload = result.get("data")
    if not payload:
        raise MvError("browser-verify-failed", "the browser produced no PDF")
    try:
        raw = base64.b64decode(payload)
    except (ValueError, TypeError) as exc:
        raise MvError("browser-verify-failed", f"the PDF is not base64: {exc}") from exc
    if not raw.startswith(b"%PDF"):
        raise MvError("browser-verify-failed", "the answer is not a PDF",
                      detail={"magic": raw[:8].hex()})
    _write_out(target, raw)
    return {"path": str(target), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "verified": True}


# --------------------------------------------------------------------------- #
# escape hatch and dialogs
# --------------------------------------------------------------------------- #

def js(cdp, sid: str, expr: str, max_result: int = MAX_RESULT,
       timeout: float | None = None) -> dict:
    value = cdp.evaluate(expr, session_id=sid, timeout=timeout)
    encoded = json.dumps(value, default=str)
    if len(encoded) > max_result:
        raise MvError("result-too-large",
                      f"the result is {len(encoded)} chars (cap {max_result})",
                      hint="narrow the expression")
    return {"result": value, "verified": False,
            "note": "the page's own value; nothing was verified"}


def dialog(cdp, sid: str, mode: str, text: str = "",
           timeout: float | None = None) -> dict:
    if mode not in ("state", "accept", "dismiss"):
        raise MvError("bad-args", f"unknown dialog action {mode!r}")
    if mode == "state":
        cdp.call("Page.enable", {}, session_id=sid)
        cdp.pump(0.4)
        opened = closed = False
        for event in cdp.events:
            if event.get("method") == "Page.javascriptDialogOpening":
                opened, closed = True, False
            elif event.get("method") == "Page.javascriptDialogClosed":
                opened, closed = False, True
        if opened:
            return {"open": True, "closed": False, "verified": True}
        if closed:
            return {"open": False, "closed": True, "verified": True}
        # No event on this connection: a renderer that will not answer proves a
        # modal dialog is up; an answering renderer only proves no dialog BLOCKS
        # it, which is not the same as a dialog never having opened.
        try:
            cdp.evaluate("1", session_id=sid, timeout=0.5)
        except MvError as exc:
            if exc.code == "eval-timeout":
                return {"open": True, "closed": False, "verified": True}
            raise
        return {"open": None, "closed": False, "verified": False,
                "note": "no dialog event was seen; null means cannot tell"}
    try:
        cdp.call("Page.enable", {}, session_id=sid)
        cdp.call("Page.handleJavaScriptDialog",
                 {"accept": mode == "accept", "promptText": text},
                 session_id=sid)
    except MvError as exc:
        if exc.code == "cdp-error" and "dialog" in exc.message.lower():
            raise MvError("no-dialog", "no JavaScript dialog is showing") from exc
        raise
    cdp.pump(0.3)
    closed = any(event.get("method") == "Page.javascriptDialogClosed"
                 for event in cdp.events)
    return {"handled": True, "closed": closed, "verified": True}


# --------------------------------------------------------------------------- #
# upload (guest-side paths)
# --------------------------------------------------------------------------- #

def upload(cdp, sid: str, guest_path: str, selector: str,
           timeout: float | None = None) -> dict:
    cdp.call("DOM.enable", {}, session_id=sid)
    document = cdp.call("DOM.getDocument", {"depth": 0}, session_id=sid)
    root = (document.get("root") or {}).get("nodeId")
    if not root:
        raise MvError("cdp-error", "the page has no document to query")
    node = cdp.call("DOM.querySelector", {"nodeId": root, "selector": selector},
                    session_id=sid)
    node_id = node.get("nodeId")
    if not node_id:
        raise MvError("no-match", f"no element matches {selector!r}")
    cdp.call("DOM.setFileInputFiles", {"files": [guest_path], "nodeId": node_id},
             session_id=sid)
    files = cdp.evaluate(
        "(() => { const el = document.querySelector(%s);"
        " if (!el || !el.files) return [];"
        " return [...el.files].map(f => ({name: f.name, size: f.size})); })()"
        % json.dumps(selector),
        session_id=sid, timeout=timeout)
    if not files:
        raise MvError("upload-not-verified",
                      f"no files landed on {selector!r}",
                      hint="check the guest path exists and the input is a file input")
    return {"files": files, "verified": True, "selector": selector}
