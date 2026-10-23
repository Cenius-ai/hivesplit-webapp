# Using HiveSplit

Every example below is copy-pasteable. `hivesplit` and `python3 hivesplit.py`
are interchangeable; the examples use the bare command.

Set-up once:

```bash
hivesplit init --apiary "Willow Yard"   # passphrase prompted twice, hidden
hivesplit unlock                        # Unlocked until <time>
```

Write commands (`hive add`, `hive status`, `split`, `queen add`,
`inspect add`, `export`) refuse to run while locked:

```
$ hivesplit hive add --label H9
Locked — run hivesplit unlock
```

Read commands (`hive list`, `hive show`, `tree`, `queen list`, `queen show`,
`inspect list`, `risk`, `status`) work at any time.

---

## 1. The apiary roster

```bash
hivesplit hive add --label H1 --kind production --yard North --established 2024-04-01
hivesplit hive list
```

```
ID  LABEL  KIND        YARD   STATUS
--  -----  ----------  -----  ------
H1  H1     production  North  active
```

* Kinds: `production`, `nuc`, `split`. A label must be unique.
* `hive show H1` prints the detail block: yard, established date, status, the
  current queen, the parent split and the children with split dates, and the last
  inspection (`No inspections yet` when there is none).
* `hive status H1 merged` retires a hive without touching its history.
  Statuses: `active`, `sold`, `dead`, `merged`.
* Hives can be addressed by id (`H1`) or by label (`Willow-01`).

Empty apiary:

```
$ hivesplit hive list
No hives yet — add one with: hivesplit hive add --label <name>
```

## 2. Splits and the lineage tree

One command creates the child hive **and** its parent edge:

```bash
hivesplit split --from H1 --label H2 --date 2025-05-10 --method walk-away
hivesplit split --from H2 --label H3 --date 2025-06-02 --method cut-down --kind nuc
hivesplit tree
```

```
H1 [H1] production active
  H2 [H2] split active — 2025-05-10 walk-away
    H3 [H3] nuc active — 2025-06-02 cut-down
```

* Methods: `walk-away`, `grafted-queen`, `cut-down`, `shook-swarm`.
* `tree --hive H2` renders only H2 and its descendants.
* `--queen Q1` installs a queen in the new hive in the same command; the queen's
  `installed_hive` moves to the child.
* The child inherits the parent's yard unless `--yard` is given, and is
  established on the split date.
* Recorded edges are append-only: there is no edit and no delete.

## 3. Queens and dam lines

```bash
hivesplit queen add --hive H1 --emerged 2025-04-01 --source grafted
hivesplit queen add --hive H2 --emerged 2025-05-01 --source supersedure-cell --mother Q1
hivesplit queen list
hivesplit queen show Q2
```

```
ID  HIVE  EMERGED     AGE   SOURCE            STATUS
--  ----  ----------  ----  ----------------  ------
Q2  H2    2025-05-01  31d   supersedure-cell  laying
Q1  H1    2025-04-01  61d   grafted           laying
```

`queen show Q2` prints the dam, the full dam line and the recorded daughters:

```
Q2 — laying queen (supersedure-cell)
Emerged  : 2025-05-01 (31d)
Source   : supersedure-cell
Status   : laying
Hive     : H2
Origin   : H1
Dam      : Q1 (H1)
Dam line : Q1 → Q2
Daughters: —
```

* Sources: `grafted`, `swarm-cell`, `supersedure-cell`, `purchased`, `unknown`.
* Statuses: `virgin`, `laying`, `retired`, `lost`, `dead` (default `laying`).
* `--origin H1` records the hive she was **produced in**; without it the origin
  defaults to the dam's hive when `--mother` is given.
* Age in days is computed from the emergence date.

## 4. Typed inspections

```bash
hivesplit inspect add --hive H2 --date 2025-06-01 \
    --queen-cells 3 --queen-cups 2 --brood-frames 6 --bee-frames 9 \
    --crowding 2 --stores 2 --temperament 1 --note "bearding at entrance"
hivesplit inspect list --hive H2
```

```
DATE        CELLS  CUPS  BROOD  BEES  CROWD  STORES  TEMP  RISK  BAND      NOTE
----------  -----  ----  -----  ----  -----  ------  ----  ----  --------  --------------------
2025-06-01      3     2      6     9      2       2     1    64  elevated  bearding at entrance
```

* One inspection per hive per date — a second one on the same date is rejected
  and nothing is written.
* `crowding`, `stores` and `temperament` are 0-3; the frame and cell counts are
  non-negative integers and default to 0.
* Every inspection snapshots its swarm-risk score, band and factor list, so a
  stored record never changes value later.

## 5. Swarm risk

```bash
hivesplit risk --hive H2
```

```
H2 — split active (H2)
Latest inspection: 2025-06-01
Swarm risk: 64 / 100 — elevated
FACTOR          POINTS  WHY
--------------  ------  ------------------
queen cells         30  3 × 10
queen cups           8  2 × 4
crowding            14  level 2 of 3
brood pressure      12  6 brood frames × 2
queen age            0  24 days old
Total: 64 / 100 — elevated
```

`hivesplit risk` with no `--hive` ranks every active hive, highest score first.

### The formula (fixed and published — audit it in `hivesplit.py`)

| Factor | Points | Notes |
| --- | --- | --- |
| queen cells | 10 each, at most 6 counted | 0-60 |
| queen cups | 4 each, at most 6 counted | 0-24 |
| crowding | 0 / 7 / 14 / 21 | level 0-3 |
| brood pressure | 2 per brood frame, at most 10 frames | 0-20 |
| brood over bees | +8 | when brood frames exceed bee frames |
| queen age | 0 under 60d, 5 from 60d, 10 from 180d, 15 from 365d | age at the inspection date |

The total is capped at 100. Bands: `low` 0-34, `elevated` 35-64, `high` 65-100.
Temperament and stores are recorded but carry no swarm weight. Identical inputs
always produce an identical score and identical factor lines.

## 6. The cadence briefing

```bash
hivesplit status            # 14-day threshold
hivesplit status --days 30  # your own threshold
```

```
Willow Yard Apiary — 6 hives, 5 queens, 6 inspections

Check first (highest swarm risk)
HIVE       ID  SCORE  BAND
---------  --  -----  --------
Willow-03  H4    100  high
...

Not inspected in 14+ days
HIVE       ID  DAYS  LAST
---------  --  ----  ----------
Nuc-B      H5   45d  2026-09-08
...

Never inspected
HIVE       ID
---------  --
Meadow-01  H3
```

An apiary with no hives prints `No hives yet` and exits 0.

## 7. Export

```bash
hivesplit export --format json                    # → stdout
hivesplit export --format csv --out hives.csv     # → file, one-line confirmation on stderr
```

* JSON: one object with `hives`, `splits`, `queens`, `inspections` (plus the
  apiary name and export timestamp).
* CSV: a header row plus one row per hive, including the parent hive and split
  date so the genealogy survives the spreadsheet.
* An empty apiary still emits valid JSON with empty arrays, or a CSV header only.
* `export` requires an unlocked session.

## 8. Machine-readable output

`--json` is available on `hive list`, `hive show`, `tree`, `queen list`,
`queen show`, `inspect list`, `risk` and `status`:

```bash
hivesplit risk --hive H2 --json
```

```json
{
  "hive": "H2",
  "label": "H2",
  "date": "2025-06-01",
  "score": 64,
  "band": "elevated",
  "factors": [ { "label": "queen cells", "points": 30, "detail": "3 × 10" }, ... ]
}
```

Human output is plain text with no ANSI colour at any time, so piping it to a
file or `less` is always safe.

## 9. The store file

`hivesplit.json` is hand-readable JSON: `keeper` (apiary name, salt, iterations,
passphrase hash), `counters`, then `hives`, `splits`, `queens`, `inspections`.
It is written atomically (temp file + `os.replace`), so an interrupted write can
never truncate a season of records.

If the file is corrupt, HiveSplit prints one line and exits 2 — never a
traceback:

```
Cannot read store ./hivesplit.json: not valid JSON — fix the file or move it aside
```

## 10. Exit codes and errors

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | domain/user error — duplicate label, unknown id, bad date, locked, wrong passphrase |
| 2 | usage error (missing argument, bad flag) or store error (missing, unreadable, unwritable) |

Every error is one plain line on stderr. A rejected command leaves the store
byte-identical.
