"""targets — the tab a caller NAMED: `id:<prefix>`, `active`, or a substring.

One resolver, one refusal vocabulary. `no-page-tab` names what exists (in
`detail.tabs`), `tab-ambiguous` names the candidates — a spec is never
resolved by picking for the caller. `active` is NOT handled here: it needs the
browser's own answer (`document.visibilityState` per tab), so the service
resolves it before calling `resolve`.
"""

from __future__ import annotations

from ..errors import MvError

__all__ = ["brief", "resolve"]


def brief(tabs: list[dict]) -> list[dict]:
    """The short shape a refusal carries: id, title, url."""
    return [{"id": tab.get("id"), "title": tab.get("title", ""),
             "url": tab.get("url", "")} for tab in tabs]


def _names(tabs: list[dict]) -> str:
    parts = []
    for tab in tabs:
        label = tab.get("title") or tab.get("url") or tab.get("id") or "?"
        parts.append(f"{label} (id:{str(tab.get('id') or '')[:8]})")
    return "; ".join(parts)


def resolve(tabs: list[dict], spec: str) -> dict:
    """The one tab `spec` names; raises on none, several, or an empty list."""
    if not tabs:
        raise MvError(
            "no-page-tab", "the browser has no page tabs open",
            hint="open one first: microvm-control browser <id> open URL",
        )
    wanted = (spec or "").strip()
    if not wanted:
        if len(tabs) == 1:
            return tabs[0]
        raise MvError(
            "tab-ambiguous",
            f"{len(tabs)} page tabs are open: {_names(tabs)}",
            hint="pass --tab id:<prefix> or a title/URL substring",
            detail={"tabs": brief(tabs)},
        )
    if wanted.startswith("id:"):
        prefix = wanted[3:].strip()
        matches = [tab for tab in tabs if str(tab.get("id") or "").startswith(prefix)]
    else:
        low = wanted.lower()
        matches = [tab for tab in tabs
                   if low in str(tab.get("title") or "").lower()
                   or low in str(tab.get("url") or "").lower()]
    if not matches:
        raise MvError(
            "no-page-tab", f"no page tab matches {wanted!r}",
            hint="microvm-control browser <id> tabs lists them",
            detail={"tabs": brief(tabs)},
        )
    if len(matches) > 1:
        raise MvError(
            "tab-ambiguous", f"{len(matches)} page tabs match {wanted!r}: {_names(matches)}",
            hint="use a longer --tab spec or id:<prefix>",
            detail={"tabs": brief(matches)},
        )
    return matches[0]
