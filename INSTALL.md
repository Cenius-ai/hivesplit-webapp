# Installing HiveSplit

HiveSplit is a single Python file with no third-party runtime dependencies. The
whole install is `python3 hivesplit.py <command>`; the steps below exist so you
get the `hivesplit` console script and the test suite too.

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python **3.9+** (`python3` on PATH) | 3.11 is what the project is developed against |
| `pip` for that interpreter | only used to install the test runner and the console script |

Nothing else. No compiler, no database, no service, no system packages —
`install.sh` never calls `sudo`, `apt`, `brew` or any package manager other than
`pip` for this project.

## 1. Install dependencies and the console script

```bash
bash install.sh
```

What it does, in order: upgrades `pip`/`setuptools`/`wheel` → installs
`requirements.txt` (the test runner, pytest) → installs the project itself
(`pip install -e .`) so `hivesplit` is on PATH → imports the module to prove the
install is complete → **exits**. It never starts a server or a background
process, and it is safe to re-run.

## 2. Create the demo apiary (optional, its own step)

```bash
bash demo.sh
```

This is the seed step. It rebuilds `demo/hivesplit.json` from scratch — six
hives, three splits, five queens, six typed inspections — and then walks every
command (`hive list`, `tree`, `queen list`, `queen show`, `inspect list`,
`risk`, `status`, `export`). It is non-interactive, needs no configuration, and
can be re-run at any time. Demo keeper passphrase: `cenius`.

## 3. Run it

```bash
# your own store, at ./hivesplit.json
python3 hivesplit.py init --apiary "Willow Yard"
python3 hivesplit.py unlock
python3 hivesplit.py hive add --label H1 --kind production --yard North

# or pick the demo store back up
HIVESPLIT_PASSPHRASE=cenius python3 hivesplit.py --store demo/hivesplit.json unlock
python3 hivesplit.py --store demo/hivesplit.json status
```

If you installed step 1, `hivesplit <command>` works as well as
`python3 hivesplit.py <command>`.

### Non-interactive passphrase

`init` and `unlock` normally prompt with a hidden prompt. For scripts and CI:

| Method | Example |
| --- | --- |
| Environment variable | `HIVESPLIT_PASSPHRASE=cenius python3 hivesplit.py unlock` |
| Piped stdin | `printf 'cenius\n' \| python3 hivesplit.py unlock` |
| Confirmation on stdin | `printf 'cenius\ncenius\n' \| python3 hivesplit.py init --apiary "Willow Yard"` |

`HIVESPLIT_PASSPHRASE` is the only environment variable HiveSplit reads. It is
optional: with no `.env` and no variable set, the interactive prompt is used and
the app is fully functional.

## 4. Tests

```bash
python3 -m pytest
```

80 end-to-end tests drive the real entry point through every command in the
plan (init/unlock, hives, splits, tree, queens, dam lines, inspections, risk,
status, export, validation and the exit-code contract). They need no network, no
services and no fixtures on disk — each test builds its store in a temp
directory.

## Files HiveSplit writes

| Path | What |
| --- | --- |
| `./hivesplit.json` (or `--store PATH`) | the store: keeper hash + hives, splits, queens, inspections |
| `./.hivesplit.session` (beside the store) | the 12-hour session; delete it to lock immediately |
| `--out FILE` on `export` | wherever you asked the export to go |

The store is mode `0600` because it holds the keeper's passphrase hash (never the
passphrase itself).
