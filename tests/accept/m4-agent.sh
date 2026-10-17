#!/usr/bin/env bash
# M4 acceptance, rootless: the agent-facing entry point.
# Verifies the skill + references are present and installed, that every CLI
# verb is documented, that every refusal code used in the package is
# documented, that the audit trail records successes and refusals, and that
# caps/doctor/dry-run reply shapes hold.
# Usage: tests/accept/m4-agent.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."

MC="$PWD/microvm-control"
WORK=$(mktemp -d "$HOME/.cache/microvm-accept-m4.XXXXXX")
export MICROVM_HOME="$WORK/home" MC
mkdir -p "$MICROVM_HOME"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

SKILL_DIR="$PWD/skills/microvm-control"
VERBS="$SKILL_DIR/references/verbs.md"
ERRORS="$SKILL_DIR/references/errors.md"
LINK="$HOME/.pi/agent/skills/microvm-control"

printf '\n== skill files\n'
for f in "$SKILL_DIR/SKILL.md" "$VERBS" "$ERRORS"; do
  [ -f "$f" ] || fail "missing $f"
done
grep -q '^name: microvm-control$' "$SKILL_DIR/SKILL.md" || fail "skill frontmatter name"
grep -q '^description: ' "$SKILL_DIR/SKILL.md" || fail "skill frontmatter description"

printf '\n== skill is installed for the agent\n'
if [ ! -e "$LINK/SKILL.md" ]; then
  mkdir -p "$(dirname "$LINK")"
  ln -sfn "$SKILL_DIR" "$LINK"
fi
[ -e "$LINK/SKILL.md" ] || fail "skill not installed at $LINK"

printf '\n== every CLI verb is documented\n'
verbs=$("$MC" --help | sed -n 's/.*{\([a-z,]*\)}.*/\1/p' | tr ',' '\n' | sort -u)
count=$(printf '%s\n' "$verbs" | grep -c .)
for v in $verbs; do
  grep -qF "\`$v\`" "$VERBS" || fail "verb '$v' is not documented in verbs.md"
done
[ "$count" -ge 20 ] || fail "unexpectedly few verbs parsed ($count)"
printf 'documented verbs: %s\n' "$count"

printf '\n== every CLI flag is documented\n'
python3 - "$MC" "$VERBS" <<'PY' || fail "undocumented CLI flags"
import re, subprocess, sys

mc, verbs_md = sys.argv[1], sys.argv[2]
doc = open(verbs_md).read()


def flags(text):
    return set(re.findall(r"(?<![\w-])--([a-z][a-z0-9-]*)", text)) - {"help"}


def help_of(*args):
    res = subprocess.run([mc, *args, "--help"], capture_output=True, text=True)
    return res.stdout + res.stderr


def doc_rows(name):
    pattern = rf"^\|\s*`{re.escape(name)}[`\s]"
    return "\n".join(line for line in doc.splitlines() if re.match(pattern, line))


top = help_of()
missing = {f"(global): --{f}" for f in flags(top) if f"`--{f}" not in doc}
verbs = re.search(r"\{([a-z,]+)\}", top).group(1).split(",")
for verb in verbs:
    text = help_of(verb)
    subs = re.search(r"\{([a-z,]+)\}", text)
    subs = subs.group(1).split(",") if subs else []
    if subs and verb == "browser":
        # `browser ID <action>`: the parser takes an id before the subcommand,
        # so the help that names the action is `browser x <action> --help` and
        # the docs rows are `browser ID <action>`.
        parent_rows = doc_rows(verb)
        missing |= {f"{verb} (legacy): --{flag}" for flag in flags(help_of(verb))
                    if f"`--{flag}" not in parent_rows}
        for sub in subs:
            label = f"{verb} ID {sub}"
            rows = doc_rows(label)
            if not rows:
                missing.add(label + ": no row")
                continue
            body = help_of(verb, "x", sub)
            missing |= {label + f": --{flag}" for flag in flags(body)
                        if f"`--{flag}" not in rows}
        continue
    for sub in subs or [""]:
        label = f"{verb} {sub}".strip()
        rows = doc_rows(label)
        if not rows:
            missing.add(label + ": no row")
            continue
        body = help_of(verb, *([sub] if sub else []))
        missing |= {label + f": --{flag}" for flag in flags(body)
                    if f"`--{flag}" not in rows}
if missing:
    print("undocumented flags: " + ", ".join(sorted(missing)), file=sys.stderr)
    sys.exit(1)
print(f"all CLI flags documented ({len(verbs)} verbs)")
PY

printf '\n== every refusal code used in the package is documented\n'
python3 - <<'PY' > "$WORK/codes.txt"
import pathlib, re
codes = set()
for path in pathlib.Path("microvm_control").rglob("*.py"):
    for match in re.finditer(r'MvError\(\s*"([a-z0-9-]+)"', path.read_text()):
        codes.add(match.group(1))
print("\n".join(sorted(codes)))
PY
undocumented=0
while read -r code; do
  grep -qF "\`$code\`" "$ERRORS" || { echo "undocumented code: $code"; undocumented=1; }
done < "$WORK/codes.txt"
[ "$undocumented" = 0 ] || fail "undocumented refusal codes"
printf 'documented codes: %s\n' "$(wc -l < "$WORK/codes.txt")"

printf '\n== doctor and caps reply shapes\n'
"$MC" image build >/dev/null
"$MC" doctor | jq -e '.ok == true and (.data.checks | length > 5)
  and ([.data.checks[] | select(.required and .ok == false)] | length == 0)' >/dev/null \
  || fail "doctor"
"$MC" caps | jq -e '.data.base.firecracker != null and .data.net.mode == "shared-tap" or .data.net.mode == "pool"' >/dev/null \
  || fail "caps"

printf '\n== state hardening\n'
[ "$(stat -c '%a' "$MICROVM_HOME")" = "700" ] || fail "state dir is not 0700"
[ "$(stat -c '%a' "$MICROVM_HOME/audit.log")" = "600" ] || fail "audit.log is not 0600"

printf '\n== instance quota\n'
echo '{"max_instances": 1}' > "$MICROVM_HOME/config.json"
"$MC" create one | jq -e '.ok' >/dev/null || fail "create one"
if "$MC" create two >"$WORK/quota.json" 2>/dev/null; then fail "quota not enforced"; fi
jq -e '.error.code == "quota-exceeded"' "$WORK/quota.json" >/dev/null || fail "wrong quota code"
"$MC" delete one --yes | jq -e '.ok' >/dev/null || fail "delete one"
rm -f "$MICROVM_HOME/config.json"

printf '\n== start refuses a missing runtime\n'
"$MC" create demo >/dev/null
mkdir -p "$WORK/no-runtime"
python3 - "$MICROVM_HOME/instances/demo/meta.json" "$WORK/no-runtime" <<'PY'
import json, sys
path, runtime = sys.argv[1], sys.argv[2]
with open(path) as handle:
    meta = json.load(handle)
meta["config"]["image_dir"] = runtime
with open(path, "w") as handle:
    json.dump(meta, handle)
PY
if "$MC" start demo >"$WORK/runtime.json" 2>/dev/null; then fail "missing runtime accepted"; fi
jq -e '.error.code == "image-missing"' "$WORK/runtime.json" >/dev/null || fail "wrong runtime code"
"$MC" delete demo --yes >/dev/null

printf '\n== doctor detects unreadable pool state\n'
echo '{}' > "$MICROVM_HOME/net.json"
chmod 000 "$MICROVM_HOME/net.json"
"$MC" doctor > "$WORK/doctor.json" 2>/dev/null || true
jq -e '[.data.checks[] | select(.name == "net-state" and .ok == false)] | length == 1' "$WORK/doctor.json" >/dev/null \
  || fail "doctor did not flag net.json"
chmod 600 "$MICROVM_HOME/net.json"
rm -f "$MICROVM_HOME/net.json"

printf '\n== dry-run contract\n'
"$MC" --dry-run create demo | jq -e '.ok == true and .data.dry_run == true and (.data.plan | length) > 0' >/dev/null \
  || fail "dry-run"
[ ! -e "$MICROVM_HOME/instances/demo" ] || fail "dry-run created an instance"

printf '\n== audit trail records successes and refusals\n'
"$MC" list >/dev/null
"$MC" status nope >/dev/null 2>&1 || true
entries=$("$MC" audit --tail 10)
echo "$entries" | jq -e '.data.entries | map(.verb) | index("list") != null' >/dev/null || fail "audit: list missing"
echo "$entries" | jq -e '.data.entries
  | map(select(.verb == "status" and .ok == false and .code == "not-found")) | length == 1' >/dev/null \
  || fail "audit: refusal missing"
echo "$entries" | jq -e '[.data.entries[] | has("ts") and has("argv") and has("duration_ms")] | all' >/dev/null \
  || fail "audit: entry fields"
echo "$entries" | jq -e '[.data.entries[] | select(.verb == "create" and .code == "dry-run" and .ok == true)] | length == 1' >/dev/null \
  || fail "audit: dry-run entry"

printf '\nM4 AGENT-ENTRY ACCEPTANCE PASSED\n'
