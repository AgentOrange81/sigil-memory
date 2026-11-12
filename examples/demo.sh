#!/usr/bin/env bash
# Build a throwaway vault, run a reflection pass with a canned response (no model, no network),
# and export both datasets. This is the fastest way to see the whole lifecycle.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
VAULT="${VAULT:-/tmp/sigil-demo}"
export VAULT

rm -rf "$VAULT"
"$PY" -m sigil --vault "$VAULT" init >/dev/null

cat > "$VAULT/transcript.md" <<'EOF'
user: the box rebooted at 03:02 and I don't know why, figure it out
assistant: boot -4 has no shutdown sequence and zero MCEs, so it is ambiguous. But boot -1 shows 649
MC25_STATUS CECC events, so the memory config is the story. Dropped DOCP 3600 -> 3200, FCLK 1600.
user: did that fix it
assistant: yes. Since the change: journalctl -k -b 0 | grep -c MC25_STATUS returns 0.
EOF

cat > "$VAULT/response.json" <<'EOF'
[{"kind": "decision",
  "summary": "DOCP 3200 with FCLK 1600 is the stable memory config on the TRX40",
  "body": "3600 produced a corrected-error storm; 3200 has run clean since.",
  "evidence": "649 MC25_STATUS CECC events in boot -1; 0 since the change",
  "importance": 0.9, "confidence": 0.95,
  "ask": "why did the box reboot at 03:02?",
  "entities": [{"name": "TRX40", "kind": "machine"}, {"name": "DOCP", "kind": "config"}],
  "edges": [{"src": "TRX40", "predicate": "measured_as", "dst": "DOCP 3200"}]},
 {"kind": "lesson",
  "summary": "An abrupt boot end with no shutdown sequence and zero MCEs is ambiguous, not a fault",
  "evidence": "boot -4 journal ends mid-input-device with no poweroff target",
  "importance": 0.7, "confidence": 0.8,
  "ask": "how do I tell a crash from a power button?"}]
EOF

"$PY" -m sigil --vault "$VAULT" reflect "$VAULT/transcript.md" \
  --fake "$VAULT/response.json" --agent glitch --project wraith

# a correction: the second boot test says 3600 was fine after the BIOS flash
OLD=$("$PY" - <<'PY'
import sqlite3, os
c = sqlite3.connect(os.environ.get("VAULT", "/tmp/sigil-demo") + "/.sigil/index.db")
print(c.execute("select id from entries where kind='decision' limit 1").fetchone()[0])
PY
)
"$PY" -m sigil --vault "$VAULT" write --kind decision --state promoted \
  --summary "DOCP 3600 is stable after the BIOS 2601 flash: 3 boots, zero MCEs" \
  --evidence "3 consecutive boots, 0 MCE lines each" \
  --ask "can I run DOCP 3600 on the TRX40 after the BIOS flash?" \
  --project wraith --agent glitch --importance 0.95 --supersedes "$OLD" \
  --edge "TRX40:measured_as:DOCP 3600"

LIVE=$("$PY" - <<'PY'
import sqlite3, os
c = sqlite3.connect(os.environ["VAULT"] + "/.sigil/index.db")
row = c.execute("select id from edges where valid_to is null and dst = 'docp-3200'").fetchone()
print(row[0] if row else "")
PY
)
if [ -n "$LIVE" ]; then
  "$PY" -m sigil --vault "$VAULT" invalidate "$LIVE"
fi
"$PY" -m sigil --vault "$VAULT" timeline trx40 || true
"$PY" -m sigil --vault "$VAULT" export sft --out "$VAULT/data/sft.jsonl"
"$PY" -m sigil --vault "$VAULT" export dpo --out "$VAULT/data/dpo.jsonl"
"$PY" -m sigil --vault "$VAULT" stats
echo "vault: $VAULT"
