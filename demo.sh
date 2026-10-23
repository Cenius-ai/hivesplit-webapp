#!/usr/bin/env bash
# HiveSplit demo — a full, non-interactive tour of the tool.
#
# The store is rebuilt from scratch on every run (demo/hivesplit.json), so this
# script is safe to run as often as you like. It never prompts, never touches
# the network and never starts anything in the background.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
STORE="demo/hivesplit.json"
SESSION="demo/.hivesplit.session"
export HIVESPLIT_PASSPHRASE="${HIVESPLIT_PASSPHRASE:-cenius}"

days_ago() {
    "$PYTHON" -c 'import datetime, sys; print((datetime.date.today() - datetime.timedelta(days=int(sys.argv[1]))).isoformat())' "$1"
}

hs() {
    printf '\n$ hivesplit --store %s %s\n' "$STORE" "$*"
    "$PYTHON" hivesplit.py --store "$STORE" "$@"
}

rm -rf demo
mkdir -p demo

cat <<'BANNER'
HiveSplit demo — Willow Yard Apiary
----------------------------------
A beekeeper's apiary genealogy: hives, splits, queens and typed inspections in
one local JSON file, with a deterministic swarm-risk score.

Demo keeper passphrase: cenius          (used by `hivesplit unlock`)
Store: demo/hivesplit.json              (rebuilt from scratch on every run)
BANNER

# 1. First run: name the apiary and set the keeper passphrase (stored only as a
#    salted PBKDF2-HMAC-SHA256 hash), then open a 12-hour write session.
hs init --apiary "Willow Yard Apiary"
hs unlock

# 2. The apiary roster.
hs hive add --label Willow-01 --kind production --yard "Willow Yard" --established "$(days_ago 1460)" --notes "two deeps, south fence line"
hs hive add --label Willow-02 --kind production --yard "Willow Yard" --established "$(days_ago 1095)" --notes "single deep plus a super"
hs hive add --label Meadow-01 --kind production --yard "Meadow Yard" --established "$(days_ago 730)" --notes "hedgerow stand, windbreak to the west"
hs hive list

# 3. Splits: each command creates the child hive and its parent edge together.
hs split --from Willow-01 --label Willow-03 --date "$(days_ago 700)" --method walk-away --notes "two queen cells left behind"
hs split --from Willow-03 --label Nuc-B --date "$(days_ago 300)" --method cut-down --kind nuc
hs split --from Meadow-01 --label Nuc-C --date "$(days_ago 120)" --method shook-swarm --notes "swarm off the apple tree"
hs tree

# 4. Queen production log, including a dam line across three generations.
hs queen add --hive Willow-01 --emerged "$(days_ago 1500)" --source purchased --status laying
hs queen add --hive Willow-02 --emerged "$(days_ago 1000)" --source grafted --mother Q1
hs queen add --hive Willow-03 --emerged "$(days_ago 700)" --source swarm-cell --mother Q1
hs queen add --hive Nuc-B --emerged "$(days_ago 90)" --source grafted --mother Q3
hs queen add --hive Nuc-C --emerged "$(days_ago 60)" --source supersedure-cell
hs queen list
hs queen show Q3

# 5. Typed inspections. The swarm-risk number is snapshotted on every record.
hs inspect add --hive Willow-01 --date "$(days_ago 95)" --queen-cups 1 --brood-frames 5 --bee-frames 8 --crowding 1 --stores 3 --temperament 1 --note "calm, stores capped"
hs inspect add --hive Willow-01 --date "$(days_ago 20)" --queen-cells 2 --queen-cups 3 --brood-frames 7 --bee-frames 9 --crowding 2 --stores 2 --temperament 1 --note "queen cups along the bottom bar, plenty of comb above"
hs inspect add --hive Willow-02 --date "$(days_ago 4)" --queen-cups 1 --brood-frames 6 --bee-frames 10 --crowding 1 --stores 3 --temperament 0 --note "boiling over the top box but no cells"
hs inspect add --hive Willow-03 --date "$(days_ago 9)" --queen-cells 5 --queen-cups 4 --brood-frames 8 --bee-frames 8 --crowding 3 --stores 1 --temperament 2 --note "bearding at dusk, cells on three frames"
hs inspect add --hive Nuc-B --date "$(days_ago 45)" --brood-frames 3 --bee-frames 4 --stores 2 --temperament 0 --note "young colony building out the nuc"
hs inspect add --hive Nuc-C --date "$(days_ago 25)" --queen-cells 1 --queen-cups 2 --brood-frames 4 --bee-frames 5 --crowding 1 --stores 1 --temperament 1 --note "light stores, feeding"
hs inspect list --hive Willow-03

# 6. The swarm-risk readout: one hive in detail, then the whole apiary ranked.
hs risk --hive Willow-03
hs risk

# 7. The one-screen briefing: overdue, never inspected, check first.
hs hive show Willow-03
hs status
hs status --days 30

# 8. Export for a spreadsheet or a backup.
hs export --format csv
hs export --format json --out demo/hivesplit-backup.json

cat <<'CLOSE'

Done. The whole apiary is one hand-readable file:
  demo/hivesplit.json            hives, splits, queens, inspections, keeper hash
  demo/hivesplit-backup.json     the JSON export written above
The CSV above went to stdout on purpose; add --out hives.csv to keep a copy.

Pick it up from here:
  python3 hivesplit.py --store demo/hivesplit.json hive list
  python3 hivesplit.py --store demo/hivesplit.json unlock   # passphrase: cenius
  python3 hivesplit.py --help
CLOSE
