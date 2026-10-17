#!/usr/bin/env bash
# M6 acceptance, rootless (no sudo): the `microvm-control browser` subcommands
# against a real guest Chromium over CDP — the vendored core in
# microvm_control/browser/, the ephemeral SSH forward, identity reconciliation,
# the legacy one-shot flags, and delete-vs-stop state handling.
# Runs one real microVM in a private user+network namespace.
# Usage: tests/accept/m6-browser.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
. "$PWD/tests/accept/lib.sh"

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept-m6.XXXXXX")
export MICROVM_HOME="$WORK/home" MC WORK
mkdir -p "$MICROVM_HOME"

cleanup() {
  kill_own_vms "$MICROVM_HOME" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

printf '\n== image build\n'
"$MC" image build | jq -e '.ok == true' >/dev/null

inner="$WORK/inner.sh"
cat > "$inner" <<'EOS'
set -euo pipefail
ip link set lo up
python3 -c "from microvm_control import netpool; print(netpool._render_script(2, 'root'))" \
  > "$WORK/net-setup.sh"
sh "$WORK/net-setup.sh" up

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "net init + create/start"
"$MC" net init --taps 2 | jq -e '.ok' >/dev/null || fail "net init"
"$MC" create src >/dev/null || fail "create"
"$MC" start src | jq -e '.verified == true' >/dev/null || fail "start"

say "fixture pages on the host side of the tap"
mkdir -p "$WORK/http"
cat > "$WORK/http/index.html" <<'HTML'
<!doctype html><html><head><title>M6 Index</title></head><body>
<h1 id="hero">hello-m6</h1>
<a id="go" href="/next.html">Go next</a>
<button id="btn">Press</button>
<input id="name">
<select id="color"><option value="r">Red</option><option value="b">Blue</option></select>
<ul><li class="item">alpha</li><li class="item">beta</li><li class="item">gamma</li></ul>
<input type="file" id="file">
<div style="height:2000px">tall</div>
</body></html>
HTML
echo '<!doctype html><html><head><title>M6 Next</title></head><body><h1>second-page</h1></body></html>' \
  > "$WORK/http/next.html"
python3 -m http.server 8000 --bind 172.16.4.1 --directory "$WORK/http" \
  </dev/null >"$WORK/http.log" 2>&1 &
HTTP=$!
trap 'kill ${HTTP:-} 2>/dev/null || true' EXIT

say "legacy one-shot flags still work"
"$MC" browser src --dom http://172.16.4.1:8000/ \
  | jq -e '.ok and .verified and (.data.dom | test("hello-m6"))' >/dev/null || fail "legacy dom"
"$MC" browser src --screenshot http://172.16.4.1:8000/ --out "$WORK/legacy.png" \
  | jq -e '.ok and .verified and .data.magic_ok == true' >/dev/null || fail "legacy screenshot"

say "no auto-start: a stopped browser refuses with a start hint"
"$MC" browser src status | jq -e '.data.running == false' >/dev/null || fail "status before start"
"$MC" browser src text >"$WORK/not-run.json" 2>/dev/null && fail "text ran without a browser" || true
jq -e '.error.code == "browser-not-running"
  and (.error.hint | test("browser src start"))' "$WORK/not-run.json" >/dev/null || fail "browser-not-running shape"

say "start (resolved port + fingerprint) and status"
if ! "$MC" browser src start >"$WORK/start.json" 2>"$WORK/start.err"; then
  cat "$WORK/start.err" >&2
  printf '%s\n' '--- guest chromium log:' >&2
  "$MC" exec src -- sh -c 'tail -30 /persist/work/browser-chromium.log 2>&1' \
    | jq -r '.data.stdout' >&2 || true
  printf '%s\n' '--- guest chromium procs:' >&2
  "$MC" exec src -- sh -c 'ps -o pid,args 2>/dev/null | grep -i chrom | grep -v grep' \
    | jq -r '.data.stdout' >&2 || true
  fail "browser start"
fi
jq -e '.ok and .verified
  and (.data.pid > 1) and (.data.port >= 1) and (.data.fingerprint.protocol | length > 0)' \
  "$WORK/start.json" >/dev/null || fail "browser start shape"
"$MC" browser src status | jq -e '.data.running == true and .data.verified == true
  and .data.tabs >= 1' >/dev/null || fail "browser status after start"
"$MC" browser src start | jq -e '.data.already_running == true' >/dev/null || fail "start is idempotent"

say "UA mitigation (windowed UA, no webdriver marker)"
"$MC" browser src js "navigator.userAgent" \
  | jq -e '.data.result | test("Chrome/[0-9]") and (test("HeadlessChrome") | not)' >/dev/null \
  || fail "user agent still announces HeadlessChrome"
"$MC" browser src js "navigator.webdriver" \
  | jq -e '.data.result == false' >/dev/null || fail "navigator.webdriver is not false"

say "open / text / find"
"$MC" browser src open http://172.16.4.1:8000/ \
  | jq -e '.ok and .verified and .data.count == 1' >/dev/null || fail "open"
"$MC" browser src text --tab active --chars 200 \
  | jq -e '.data.text | test("hello-m6")' >/dev/null || fail "text"
"$MC" browser src find --selector '#go' \
  | jq -e '.data.count == 1 and .data.matches[0].hit == true' >/dev/null || fail "find"

say "click a link (verified by the move)"
"$MC" browser src click --selector '#go' \
  | jq -e '.data.clicked and .data.changed
    and (.data.after.url | test("next.html"))' >/dev/null || fail "click + move"

say "nav / focus / insert / select"
"$MC" browser src nav http://172.16.4.1:8000/ \
  | jq -e '.data.url_read | test("8000/$")' >/dev/null || fail "nav"
"$MC" browser src focus --selector '#name' >"$WORK/focus.json" || fail "focus"
jq -e '.data.focused' "$WORK/focus.json" >/dev/null \
  || { cat "$WORK/focus.json"; fail "focus shape"; }
"$MC" browser src insert "Ada" >"$WORK/insert.json" || { cat "$WORK/insert.json"; fail "insert refused"; }
jq -e '.verified and .data.length_before == 0 and .data.length_after == 3' "$WORK/insert.json" >/dev/null \
  || { cat "$WORK/insert.json"; fail "insert not verified"; }
"$MC" browser src type "!!" --delay 0.05 >"$WORK/type.json" || { cat "$WORK/type.json"; fail "type refused"; }
jq -e '.verified and .data.length_after == 5 and .data.delay_s == 0.05' "$WORK/type.json" >/dev/null \
  || { cat "$WORK/type.json"; fail "type with delay not verified"; }
"$MC" browser src select --selector '#color' --value Blue \
  | jq -e '.data.selected and (.data.by == "label" or .data.by == "value")' >/dev/null || fail "select"
"$MC" browser src scroll --by 200 | jq -e '.data.moved == true' >/dev/null || fail "scroll"

say "wait / extract / js"
"$MC" browser src wait --for element --selector '#hero' --timeout 5 \
  | jq -e '.data.for == "element"' >/dev/null || fail "wait"
"$MC" browser src extract --each '.item' --field text=:scope \
  | jq -e '.data.count == 3 and .data.matches[0].text == "alpha"' >/dev/null || fail "extract"
"$MC" browser src js "1 + 1" \
  | jq -e '.data.result == 2 and .verified == false' >/dev/null || fail "js"

say "upload a guest path"
"$MC" exec src -- sh -c 'echo upload-body > /persist/work/up.txt' >/dev/null
"$MC" browser src upload /persist/work/up.txt --selector '#file' \
  | jq -e '.verified and .data.files[0].name == "up.txt"' >/dev/null || fail "upload"
"$MC" browser src upload /persist/work/missing.txt --selector '#file' \
  >"$WORK/nofile.json" 2>/dev/null && fail "upload of a missing path" || true
jq -e '.error.code == "no-file"' "$WORK/nofile.json" >/dev/null || fail "no-file shape"

say "screenshot (IHDR-verified) and pdf"
"$MC" browser src screenshot --out "$WORK/shot.png" --selector '#hero' \
  | jq -e '.verified and .data.mode == "element" and (.data.width > 0)' >/dev/null || fail "screenshot"
head -c8 "$WORK/shot.png" | od -An -tx1 | grep -q '89 50 4e 47' || fail "PNG magic"
"$MC" browser src pdf --out "$WORK/page.pdf" \
  | jq -e '.verified and (.data.bytes > 0)' >/dev/null || fail "pdf"
head -c4 "$WORK/page.pdf" | grep -q '%PDF' || fail "PDF magic"
"$MC" browser src screenshot --out "$WORK/shot.png" >"$WORK/exists.json" 2>/dev/null \
  && fail "screenshot overwrote without --force" || true
jq -e '.error.code == "file-exists"' "$WORK/exists.json" >/dev/null || fail "file-exists shape"

say "tabs + close"
"$MC" browser src open http://172.16.4.1:8000/next.html >/dev/null || fail "open second tab"
"$MC" browser src tabs | jq -e '.data.count == 2' >/dev/null || fail "two tabs"
TAB=$("$MC" browser src tabs | jq -r '.data.tabs[-1].id')
"$MC" browser src close "id:$TAB" | jq -e '.data.count == 1' >/dev/null || fail "close tab"
"$MC" browser src tabs | jq -e '.data.count == 1' >/dev/null || fail "close left one tab"

say "identity: a tampered pid is reconciled away"
python3 - "$MICROVM_HOME/instances/src/browser.json" <<'PY'
import json, sys
path = sys.argv[1]
record = json.load(open(path))
record["pid"] = 1
json.dump(record, open(path, "w"))
PY
"$MC" browser src status | jq -e '.data.running == false' >/dev/null || fail "stale pid reconciled"
[ ! -e "$MICROVM_HOME/instances/src/browser.json" ] || fail "stale browser.json not cleared"

say "VM stop does not clear the file; the next call reconciles"
"$MC" browser src start >/dev/null
[ -e "$MICROVM_HOME/instances/src/browser.json" ] || fail "start did not record state"
"$MC" stop src >/dev/null
[ -e "$MICROVM_HOME/instances/src/browser.json" ] || fail "VM stop cleared browser.json too early"
"$MC" start src >/dev/null || fail "restart VM"
"$MC" browser src status | jq -e '.data.running == false' >/dev/null || fail "dead guest pid not reconciled"

say "delete clears browser.json with the instance"
"$MC" browser src start >/dev/null
TRASH=$("$MC" delete src --yes | jq -r '.data.trash_path // empty')
[ -n "$TRASH" ] || fail "delete produced no trash path"
[ ! -e "$TRASH/browser.json" ] || fail "trashed instance still carries browser.json"
"$MC" list | jq -e '.data.count == 0' >/dev/null || fail "instance list after delete"

printf '\nM6 BROWSER ACCEPTANCE PASSED\n'
EOS

unshare -Urn bash "$inner"
