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
  count=$((count + 1))
done
[ "$count" -ge 20 ] || fail "unexpectedly few verbs parsed ($count)"
printf 'documented verbs: %s\n' "$count"

printf '\n== every refusal code used in the package is documented\n'
python3 - <<'PY' > "$WORK/codes.txt"
import pathlib, re
codes = set()
for path in pathlib.Path("microvm_control").glob("*.py"):
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
