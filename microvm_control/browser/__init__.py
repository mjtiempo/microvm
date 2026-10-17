"""browser — the vendored headless-Chrome control core (see PLAN-browser.md).

Extracted from `mark-browser-control` (MIT, Copyright (c) 2026 Mark Tiempo)
and adapted to microvm-control's transport (an ephemeral SSH forward into the
guest), state (`browser.json`) and error envelope (`MvError`). See
`NOTICE` in this package for the provenance statement.

Modules:

* `ws`       — stdlib RFC 6455 client (the host has no third-party deps)
* `cdp`      — HTTP `/json` reads + a synchronous CDP connection over `ws`
* `targets`  — tab resolution (`id:` prefix, `active`, title/URL substring)
* `state`    — `browser.json`: load, save, and the pure staleness verdict
* `dom`      — the page verbs (text, find, extract, click, type, …)
* `service`  — guest lifecycle (start/stop/status), the SSH forward, the
               identity gate, and the verb dispatch behind `microvm-control
               browser`
"""
