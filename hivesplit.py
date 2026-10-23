#!/usr/bin/env python3
"""HiveSplit — an apiary hive split genealogy tracker.

HiveSplit keeps one beekeeper's apiary in a single human-readable JSON file:
the hives, the parent -> child edge every split creates, the queens with their
production (emergence) date and source, and typed inspections whose fields are
turned into a deterministic 0-100 swarm-risk score with a per-factor
breakdown.

Standard library only. No network, no server, no background process, no build
step: ``python3 hivesplit.py <command>`` is the entire install.

Read commands (list/show/tree/risk/status) run while locked; every write
command and ``export`` needs a session opened by ``hivesplit unlock``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# Constants a keeper can audit
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1

DEFAULT_STORE = "hivesplit.json"
SESSION_FILENAME = ".hivesplit.session"
SESSION_HOURS = 12
PASSPHRASE_ENV = "HIVESPLIT_PASSPHRASE"
MIN_PASSPHRASE_LENGTH = 4

PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16

DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

HIVE_KINDS = ("production", "nuc", "split")
HIVE_STATUSES = ("active", "sold", "dead", "merged")
SPLIT_METHODS = ("walk-away", "grafted-queen", "cut-down", "shook-swarm")
QUEEN_SOURCES = ("grafted", "swarm-cell", "supersedure-cell", "purchased", "unknown")
QUEEN_STATUSES = ("virgin", "laying", "retired", "lost", "dead")

COLLECTIONS = ("hives", "queens", "splits", "inspections")

# Swarm-risk formula. Every weight is a named constant: a keeper can read the
# numbers here and reproduce the score on paper.
#
#   queen cells     10 points each, at most 6 cells counted  (30 -> 60)
#   queen cups       4 points each, at most 6 cups counted   (0 -> 24)
#   crowding         0/7/14/21 for level 0/1/2/3
#   brood pressure   2 points per brood frame, at most 10 frames (0 -> 20)
#                    plus 8 points when brood outruns the adult bee frames
#   queen age        5 points from 60 days, 10 from 180, 15 from 365
#
# The total is capped at 100. Temperament and stores are recorded but carry no
# swarm weight — a defensive colony or a light store is not a swarming signal.
QUEEN_CELL_POINTS = 10
QUEEN_CELL_CAP = 6
QUEEN_CUP_POINTS = 4
QUEEN_CUP_CAP = 6
CROWDING_POINTS = (0, 7, 14, 21)
BROOD_POINTS_PER_FRAME = 2
BROOD_FRAME_CAP = 10
BROOD_OVER_BEES_BONUS = 8
QUEEN_AGE_STEPS = ((365, 15), (180, 10), (60, 5))
RISK_MAX = 100
RISK_HIGH_FROM = 65
RISK_ELEVATED_FROM = 35

EXIT_OK = 0
EXIT_USER = 1
EXIT_USAGE = 2

EMPTY = "—"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class HiveSplitError(Exception):
    """A domain or user error: printed as one plain line, exit code 1."""


class StoreError(HiveSplitError):
    """A store problem (missing, unreadable, unwritable): exit code 2."""


# --------------------------------------------------------------------------
# Validation layer
# --------------------------------------------------------------------------


def parse_date(value: str, label: str, *, echo_value: bool = False) -> dt.date:
    """Validate a YYYY-MM-DD calendar date."""
    text = (value or "").strip()
    try:
        parsed = dt.datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        parsed = None
    if parsed is None or len(text) != 10:
        if echo_value and parsed is None and len(text) == 10:
            raise HiveSplitError(f"{text} is not a real date")
        raise HiveSplitError(f"{label} must be a real date in YYYY-MM-DD form")
    return parsed


def parse_enum(value: str, allowed: tuple, label: str) -> str:
    text = (value or "").strip().lower()
    if text not in allowed:
        raise HiveSplitError(f"{label} must be one of: {', '.join(allowed)}")
    return text


def parse_int(value: str, label: str, minimum: int = 0) -> int:
    text = str(value if value is not None else "").strip()
    try:
        number = int(text)
    except ValueError:
        raise HiveSplitError(f"{label} must be a whole number") from None
    if number < minimum:
        raise HiveSplitError(f"{label} must be {minimum} or more")
    return number


def parse_level(value: str, label: str) -> int:
    number = parse_int(value, label)
    if number > 3:
        raise HiveSplitError(f"{label} must be 0-3")
    return number


def require_text(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise HiveSplitError(f"{label} cannot be empty")
    return text


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def today() -> dt.date:
    return dt.date.today()


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).strftime(DATETIME_FORMAT)


def as_date(value: str) -> dt.date:
    return dt.datetime.strptime(str(value), DATE_FORMAT).date()


def days_between(start: dt.date, end: dt.date) -> int:
    return (end - start).days


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def truncate(text: str, width: int = 34) -> str:
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Atomic JSON store
# --------------------------------------------------------------------------


def new_document(keeper: dict) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "keeper": keeper,
        "counters": {"hive": 0, "queen": 0, "split": 0, "inspection": 0},
        "hives": [],
        "queens": [],
        "splits": [],
        "inspections": [],
    }


def _read_document(path: Path) -> dict:
    """Parse the store file as JSON data with json.loads — data only, no code."""
    if not path.exists():
        raise StoreError(f"No store at {path} — run hivesplit init")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StoreError(f"Cannot read store {path}: {exc.strerror or exc}") from None
    try:
        document = json.loads(raw)
    except ValueError:
        raise StoreError(
            f"Cannot read store {path}: not valid JSON — fix the file or move it aside"
        ) from None
    if not isinstance(document, dict):
        raise StoreError(
            f"Cannot read store {path}: unexpected structure — fix the file or move it aside"
        )
    if not isinstance(document.get("keeper"), dict):
        raise StoreError(
            f"Cannot read store {path}: no keeper record — run hivesplit init"
        )
    for key in COLLECTIONS:
        if not isinstance(document.get(key), list):
            raise StoreError(
                f"Cannot read store {path}: '{key}' is missing — fix the file or move it aside"
            )
    document.setdefault("counters", {})
    return document


class Store:
    """One JSON document loaded at command start, written atomically at the end."""

    def __init__(self, path: Path, document: dict) -> None:
        self.path = path
        self.data = document

    @classmethod
    def open(cls, path: Path) -> "Store":
        """Read the store: plain JSON parsed as data with json.loads."""
        return cls(path, _read_document(path))

    @classmethod
    def create(cls, path: Path, keeper: dict) -> "Store":
        return cls(path, new_document(keeper))

    # -- persistence -------------------------------------------------------
    def save(self) -> None:
        """Write to a temp file in the same directory, then os.replace over the store."""
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(
                f"Cannot write store {self.path}: {exc.strerror or exc}"
            ) from None
        payload = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        handle = None
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(directory), prefix=".hivesplit-", suffix=".tmp"
            )
            handle = os.fdopen(fd, "w", encoding="utf-8")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(tmp_name, self.path)
            tmp_name = None
        except OSError as exc:
            raise StoreError(
                f"Cannot write store {self.path}: {exc.strerror or exc}"
            ) from None
        finally:
            if handle is not None:
                handle.close()
            if tmp_name is not None and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    # -- lookups -----------------------------------------------------------
    @property
    def keeper(self) -> dict:
        return self.data["keeper"]

    @property
    def apiary_name(self) -> str:
        return str(self.keeper.get("apiary_name", "apiary"))

    def hives(self) -> list:
        return self.data["hives"]

    def queens(self) -> list:
        return self.data["queens"]

    def splits(self) -> list:
        return self.data["splits"]

    def inspections(self) -> list:
        return self.data["inspections"]

    def next_id(self, collection: str, prefix: str) -> str:
        counters = self.data.setdefault("counters", {})
        taken = {row.get("id") for row in self.data.get(collection, [])}
        number = int(counters.get(collection, 0))
        while True:
            number += 1
            candidate = f"{prefix}{number}"
            if candidate not in taken:
                break
        counters[collection] = number
        return candidate

    def resolve_hive(self, token: str) -> dict:
        text = (token or "").strip()
        for hive in self.hives():
            if hive.get("id") == text:
                return hive
        for hive in self.hives():
            if hive.get("label") == text:
                return hive
        raise HiveSplitError(f"No hive {text}")

    def hive_or_none(self, hive_id: str):
        for hive in self.hives():
            if hive.get("id") == hive_id:
                return hive
        return None

    def resolve_queen(self, token: str) -> dict:
        text = (token or "").strip()
        for queen in self.queens():
            if queen.get("id") == text:
                return queen
        raise HiveSplitError(f"No queen {text}")

    def queen_or_none(self, queen_id: str):
        for queen in self.queens():
            if queen.get("id") == queen_id:
                return queen
        return None

    def hive_label(self, hive_id: str) -> str:
        hive = self.hive_or_none(hive_id)
        return hive.get("label", hive_id) if hive else str(hive_id)

    def parent_split(self, hive_id: str):
        for split in self.splits():
            if split.get("child_hive_id") == hive_id:
                return split
        return None

    def child_splits(self, hive_id: str) -> list:
        return [s for s in self.splits() if s.get("parent_hive_id") == hive_id]

    def inspections_for(self, hive_id: str) -> list:
        rows = [i for i in self.inspections() if i.get("hive_id") == hive_id]
        return sorted(rows, key=lambda row: (row.get("date", ""), row.get("id", "")), reverse=True)

    def latest_inspection(self, hive_id: str):
        rows = self.inspections_for(hive_id)
        return rows[0] if rows else None

    def current_queen(self, hive: dict):
        queen_id = hive.get("current_queen_id")
        return self.queen_or_none(queen_id) if queen_id else None


# --------------------------------------------------------------------------
# Keeper passphrase + session
# --------------------------------------------------------------------------


def hash_passphrase(passphrase: str, salt_hex: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        passphrase.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return digest.hex()


def read_passphrase(*, confirm: bool) -> str:
    """Take the passphrase from env, a pipe, or a hidden prompt — never echoed."""
    from_env = os.environ.get(PASSPHRASE_ENV)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        line = sys.stdin.readline().strip()
        if not line:
            raise HiveSplitError(
                f"No passphrase supplied — set {PASSPHRASE_ENV} or pipe it on stdin"
            )
        if confirm:
            # A piped confirmation line (printf "one\ntwo\n") is compared when
            # present; a single line is accepted as the passphrase on its own.
            second = sys.stdin.readline().strip()
            if second and not hmac.compare_digest(line, second):
                raise HiveSplitError("Passphrases do not match")
        return line
    first = getpass.getpass("Passphrase: ")
    if confirm:
        second = getpass.getpass("Repeat passphrase: ")
        if not hmac.compare_digest(first, second):
            raise HiveSplitError("Passphrases do not match")
    if not first:
        raise HiveSplitError("passphrase cannot be empty")
    return first


def session_path_for(store_path: Path, override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    return store_path.parent / SESSION_FILENAME


def write_session(path: Path, store: Store, expires_at: dt.datetime) -> None:
    payload = {
        "keeper_id": store.keeper.get("keeper_id", ""),
        "store": str(store.path.resolve()),
        "created_at": now_iso(),
        "expires_at": expires_at.replace(microsecond=0).strftime(DATETIME_FORMAT),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".hivesplit-session-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except OSError as exc:
        raise StoreError(
            f"Cannot write session {path}: {exc.strerror or exc}"
        ) from None
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def read_session(path: Path) -> dict | None:
    """Parse the session file as JSON data with json.loads — data only, no code."""
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def session_is_live(store: Store, session: dict | None) -> bool:
    if not session:
        return False
    if session.get("keeper_id") != store.keeper.get("keeper_id"):
        return False
    recorded_store = session.get("store")
    if recorded_store and Path(recorded_store) != store.path.resolve():
        return False
    try:
        expires = dt.datetime.strptime(str(session.get("expires_at")), DATETIME_FORMAT)
    except (TypeError, ValueError):
        return False
    return expires > dt.datetime.now()


def require_unlocked(store: Store, session_file: Path) -> None:
    if not session_is_live(store, read_session(session_file)):
        raise HiveSplitError("Locked — run hivesplit unlock")


# --------------------------------------------------------------------------
# Swarm-risk formula
# --------------------------------------------------------------------------


def queen_age_points(age_days: int | None) -> tuple:
    if age_days is None:
        return 0, "no queen on record"
    for threshold, points in QUEEN_AGE_STEPS:
        if age_days >= threshold:
            return points, f"{age_days} days old"
    return 0, f"{age_days} days old"


def compute_risk(inspection: dict, queen: dict | None) -> dict:
    """Deterministic 0-100 score plus the per-factor breakdown that produced it."""
    factors = []

    cells = int(inspection.get("queen_cells", 0))
    counted_cells = min(cells, QUEEN_CELL_CAP)
    detail = f"{counted_cells} × {QUEEN_CELL_POINTS}"
    if cells > QUEEN_CELL_CAP:
        detail += f" (capped from {cells})"
    factors.append(
        {"label": "queen cells", "points": counted_cells * QUEEN_CELL_POINTS, "detail": detail}
    )

    cups = int(inspection.get("queen_cups", 0))
    counted_cups = min(cups, QUEEN_CUP_CAP)
    detail = f"{counted_cups} × {QUEEN_CUP_POINTS}"
    if cups > QUEEN_CUP_CAP:
        detail += f" (capped from {cups})"
    factors.append(
        {"label": "queen cups", "points": counted_cups * QUEEN_CUP_POINTS, "detail": detail}
    )

    crowding = max(0, min(int(inspection.get("crowding", 0)), 3))
    factors.append(
        {
            "label": "crowding",
            "points": CROWDING_POINTS[crowding],
            "detail": f"level {crowding} of 3",
        }
    )

    brood = max(0, int(inspection.get("brood_frames", 0)))
    brood_points = min(brood, BROOD_FRAME_CAP) * BROOD_POINTS_PER_FRAME
    detail = f"{min(brood, BROOD_FRAME_CAP)} brood frames × {BROOD_POINTS_PER_FRAME}"
    if brood > int(inspection.get("bee_frames", 0)):
        brood_points += BROOD_OVER_BEES_BONUS
        detail += f" + {BROOD_OVER_BEES_BONUS} (brood outruns bees)"
    factors.append({"label": "brood pressure", "points": brood_points, "detail": detail})

    age_days = None
    if queen is not None:
        emerged = queen.get("emerged_date")
        inspection_date = inspection.get("date")
        if emerged and inspection_date:
            age_days = max(0, days_between(as_date(emerged), as_date(inspection_date)))
        else:
            age_days = 0
    age_points, age_detail = queen_age_points(age_days)
    factors.append({"label": "queen age", "points": age_points, "detail": age_detail})

    score = min(RISK_MAX, sum(factor["points"] for factor in factors))
    band = risk_band(score)
    return {"score": score, "band": band, "factors": factors, "queen_age_days": age_days}


def risk_band(score: int) -> str:
    if score >= RISK_HIGH_FROM:
        return "high"
    if score >= RISK_ELEVATED_FROM:
        return "elevated"
    return "low"


# --------------------------------------------------------------------------
# Rendering helpers (clean & minimal, columns sized to content)
# --------------------------------------------------------------------------

COLUMN_GAP = "  "


def render_table(headers: list, rows: list, aligns: list | None = None) -> str:
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def format_row(cells: list) -> str:
        parts = []
        for index, cell in enumerate(cells):
            text = str(cell)
            if aligns and aligns[index] == "right":
                parts.append(text.rjust(widths[index]))
            else:
                parts.append(text.ljust(widths[index]))
        return COLUMN_GAP.join(parts).rstrip()

    lines = [format_row([str(header).upper() for header in headers])]
    lines.append(COLUMN_GAP.join("-" * width for width in widths))
    lines.extend(format_row([str(cell) for cell in row]) for row in rows)
    return "\n".join(lines)


def render_fields(pairs: list) -> str:
    width = max((len(label) for label, _ in pairs), default=0)
    return "\n".join(f"{label.ljust(width)}: {value}" for label, value in pairs)


def emit(text: str) -> None:
    """Write a block to stdout with exactly one trailing newline."""
    sys.stdout.write(text.rstrip("\n") + "\n")


def emit_json(payload) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Views shared by several commands
# --------------------------------------------------------------------------


def hive_row(hive: dict) -> list:
    return [
        hive.get("id", ""),
        hive.get("label", ""),
        hive.get("kind", ""),
        hive.get("yard") or EMPTY,
        hive.get("status", ""),
    ]


def queen_row(queen: dict, store: Store) -> list:
    emerged = queen.get("emerged_date", "")
    try:
        age = days_between(as_date(emerged), today())
    except ValueError:
        age = 0
    return [
        queen.get("id", ""),
        store.hive_label(queen.get("installed_hive_id") or ""),
        emerged,
        f"{age}d",
        queen.get("source", ""),
        queen.get("status", ""),
    ]


def inspection_row(inspection: dict) -> list:
    return [
        inspection.get("date", ""),
        inspection.get("queen_cells", 0),
        inspection.get("queen_cups", 0),
        inspection.get("brood_frames", 0),
        inspection.get("bee_frames", 0),
        inspection.get("crowding", 0),
        inspection.get("stores", 0),
        inspection.get("temperament", 0),
        inspection.get("swarm_risk", 0),
        inspection.get("risk_band", ""),
        truncate(inspection.get("note", "")) or EMPTY,
    ]


def dam_chain(store: Store, queen: dict) -> list:
    """Root-to-queen list of ancestors, following mother links."""
    chain = []
    seen = set()
    current = queen
    while current is not None:
        queen_id = current.get("id")
        if queen_id in seen:
            break
        seen.add(queen_id)
        chain.append(current)
        mother_id = current.get("mother_queen_id")
        current = store.queen_or_none(mother_id) if mother_id else None
    chain.reverse()
    return chain


def daughters_of(store: Store, queen_id: str) -> list:
    return [q for q in store.queens() if q.get("mother_queen_id") == queen_id]


# --------------------------------------------------------------------------
# Commands: init / unlock
# --------------------------------------------------------------------------


def cmd_init(args, store_path: Path, session_file: Path) -> int:
    if store_path.exists():
        raise HiveSplitError(f"Store already exists at {store_path}")
    apiary = require_text(args.apiary, "apiary")
    passphrase = read_passphrase(confirm=True)
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise HiveSplitError(
            f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters"
        )
    salt = secrets.token_hex(SALT_BYTES)
    keeper = {
        "keeper_id": "K1",
        "apiary_name": apiary,
        "passphrase_hash": hash_passphrase(passphrase, salt, PBKDF2_ITERATIONS),
        "salt": salt,
        "iterations": PBKDF2_ITERATIONS,
        "created_at": now_iso(),
    }
    store = Store.create(store_path, keeper)
    store.save()
    emit(f'Apiary "{apiary}" created.')
    emit(f"Store: {store_path}")
    emit("Next: hivesplit unlock")
    return EXIT_OK


def cmd_unlock(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    passphrase = read_passphrase(confirm=False)
    keeper = store.keeper
    expected = str(keeper.get("passphrase_hash", ""))
    salt = str(keeper.get("salt", ""))
    iterations = int(keeper.get("iterations", PBKDF2_ITERATIONS))
    try:
        candidate = hash_passphrase(passphrase, salt, iterations)
    except ValueError:
        raise HiveSplitError("Passphrase does not match") from None
    if not hmac.compare_digest(candidate, expected):
        raise HiveSplitError("Passphrase does not match")
    expires = dt.datetime.now() + dt.timedelta(hours=SESSION_HOURS)
    write_session(session_file, store, expires)
    emit(f"Unlocked until {expires.strftime(DATETIME_FORMAT)}")
    emit(f"Store: {store_path}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: hive
# --------------------------------------------------------------------------


def cmd_hive_add(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    require_unlocked(store, session_file)
    label = require_text(args.label, "label")
    kind = parse_enum(args.kind, HIVE_KINDS, "kind")
    if any(hive.get("label") == label for hive in store.hives()):
        raise HiveSplitError(f"A hive labelled {label} already exists")
    if args.established:
        established = parse_date(args.established, "established", echo_value=True).strftime(DATE_FORMAT)
    else:
        established = today().strftime(DATE_FORMAT)
    hive = {
        "id": store.next_id("hives", "H"),
        "label": label,
        "kind": kind,
        "yard": (args.yard or "").strip(),
        "established_date": established,
        "status": "active",
        "current_queen_id": None,
        "notes": (args.notes or "").strip(),
        "created_at": now_iso(),
    }
    store.hives().append(hive)
    store.save()
    emit(f"Added hive {hive['id']} ({hive['label']}, {hive['kind']}) to {store.apiary_name}.")
    return EXIT_OK


def cmd_hive_list(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    hives = sorted(store.hives(), key=lambda row: (_id_number(row.get("id")), row.get("label", "")))
    if args.json:
        emit_json(hives)
        return EXIT_OK
    if not hives:
        emit("No hives yet — add one with: hivesplit hive add --label <name>")
        return EXIT_OK
    rows = [hive_row(hive) for hive in hives]
    emit(render_table(["id", "label", "kind", "yard", "status"], rows))
    return EXIT_OK


def _id_number(value) -> int:
    text = str(value or "")
    digits = "".join(char for char in text if char.isdigit())
    return int(digits) if digits else 0


def cmd_hive_show(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    hive = store.resolve_hive(args.hive)
    if args.json:
        payload = dict(hive)
        queen = store.current_queen(hive)
        payload["current_queen"] = queen
        payload["parent"] = store.parent_split(hive["id"])
        payload["children"] = store.child_splits(hive["id"])
        payload["last_inspection"] = store.latest_inspection(hive["id"])
        emit_json(payload)
        return EXIT_OK

    lines = [f"{hive.get('label')} — {hive.get('kind')} ({hive.get('id')})"]
    fields = [
        ("Yard", hive.get("yard") or EMPTY),
        ("Established", hive.get("established_date", "")),
        ("Status", hive.get("status", "")),
    ]
    queen = store.current_queen(hive)
    if queen:
        fields.append(
            (
                "Queen",
                f"{queen.get('id')} ({queen.get('status')}, emerged {queen.get('emerged_date')})",
            )
        )
    else:
        fields.append(("Queen", EMPTY))

    parent = store.parent_split(hive["id"])
    if parent:
        fields.append(
            (
                "Parent",
                f"{store.hive_label(parent.get('parent_hive_id'))} "
                f"({parent.get('date')} {parent.get('method')})",
            )
        )
    else:
        fields.append(("Parent", EMPTY))

    children = store.child_splits(hive["id"])
    if children:
        pairs = ", ".join(
            f"{store.hive_label(split.get('child_hive_id'))} ({split.get('date')})"
            for split in sorted(children, key=lambda row: row.get("date", ""))
        )
        fields.append(("Children", pairs))
    else:
        fields.append(("Children", EMPTY))

    latest = store.latest_inspection(hive["id"])
    if latest:
        age = days_between(as_date(latest["date"]), today())
        fields.append(
            (
                "Last inspection",
                f"{latest['date']} ({age}d ago) — risk {latest.get('swarm_risk')} {latest.get('risk_band')}",
            )
        )
    else:
        fields.append(("Last inspection", "No inspections yet"))

    if hive.get("notes"):
        fields.append(("Notes", hive["notes"]))
    lines.append(render_fields(fields))
    emit("\n".join(lines))
    return EXIT_OK


def cmd_hive_status(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    require_unlocked(store, session_file)
    hive = store.resolve_hive(args.hive)
    status = parse_enum(args.status, HIVE_STATUSES, "status")
    previous = hive.get("status")
    if previous != status:
        hive["status"] = status
        store.save()
    emit(f"{hive.get('label')} ({hive.get('id')}): {previous} → {status}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: split / tree
# --------------------------------------------------------------------------


def cmd_split(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    require_unlocked(store, session_file)
    parent = store.resolve_hive(args.parent)
    label = require_text(args.label, "label")
    split_date = parse_date(args.date, "date", echo_value=True).strftime(DATE_FORMAT)
    method = parse_enum(args.method, SPLIT_METHODS, "method")
    kind = parse_enum(args.kind, HIVE_KINDS, "kind")
    if any(hive.get("label") == label for hive in store.hives()):
        raise HiveSplitError(f"A hive labelled {label} already exists")
    queen = store.resolve_queen(args.queen) if args.queen else None

    yard = (args.yard or "").strip() or parent.get("yard", "")
    child = {
        "id": store.next_id("hives", "H"),
        "label": label,
        "kind": kind,
        "yard": yard,
        "established_date": split_date,
        "status": "active",
        "current_queen_id": None,
        # The note on a split describes the split itself, so it is stored on the
        # edge rather than copied onto the new hive.
        "notes": "",
        "created_at": now_iso(),
    }
    store.hives().append(child)

    split = {
        "id": store.next_id("splits", "S"),
        "parent_hive_id": parent["id"],
        "child_hive_id": child["id"],
        "date": split_date,
        "method": method,
        "queen_id": queen["id"] if queen else None,
        "notes": (args.notes or "").strip(),
    }
    store.splits().append(split)

    if queen is not None:
        queen["installed_hive_id"] = child["id"]
        child["current_queen_id"] = queen["id"]
        store.save()
        emit(
            f"Split {split['id']}: {parent.get('label')} → {child.get('label')} "
            f"on {split_date} ({method}), queen {queen['id']} attached."
        )
    else:
        store.save()
        emit(
            f"Split {split['id']}: {parent.get('label')} → {child.get('label')} "
            f"on {split_date} ({method})."
        )
    return EXIT_OK


def _tree_lines(store: Store, hive: dict, depth: int, lines: list, seen: set) -> None:
    if hive["id"] in seen:
        return
    seen.add(hive["id"])
    indent = "  " * depth
    parent_split = store.parent_split(hive["id"])
    suffix = ""
    if parent_split:
        suffix = f" — {parent_split.get('date')} {parent_split.get('method')}"
    lines.append(
        f"{indent}{hive.get('label')} [{hive.get('id')}] "
        f"{hive.get('kind')} {hive.get('status')}{suffix}"
    )
    children = sorted(
        store.child_splits(hive["id"]),
        key=lambda row: (row.get("date", ""), _id_number(row.get("id"))),
    )
    for split in children:
        child = store.hive_or_none(split.get("child_hive_id"))
        if child is not None:
            _tree_lines(store, child, depth + 1, lines, seen)


def cmd_tree(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    if not store.hives():
        if args.json:
            emit_json([])
        else:
            emit("No hives yet")
        return EXIT_OK

    roots = [hive for hive in store.hives() if store.parent_split(hive["id"]) is None]
    if args.hive:
        root = store.resolve_hive(args.hive)
    else:
        root = None
    lines: list = []
    seen: set = set()
    if root is not None:
        _tree_lines(store, root, 0, lines, seen)
    else:
        for hive in sorted(roots, key=lambda row: _id_number(row.get("id"))):
            _tree_lines(store, hive, 0, lines, seen)

    if args.json:
        emit_json({"lines": lines, "splits": len(store.splits())})
        return EXIT_OK
    emit("\n".join(lines))
    if not store.splits():
        emit("No splits recorded yet — record one with: hivesplit split")
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: queen
# --------------------------------------------------------------------------


def cmd_queen_add(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    require_unlocked(store, session_file)
    hive = store.resolve_hive(args.hive)
    emerged = parse_date(args.emerged, "emerged", echo_value=True).strftime(DATE_FORMAT)
    source = parse_enum(args.source, QUEEN_SOURCES, "source")
    status = parse_enum(args.status, QUEEN_STATUSES, "status")
    origin = store.resolve_hive(args.origin) if args.origin else None
    mother = store.resolve_queen(args.mother) if args.mother else None
    if mother is not None and origin is None and mother.get("installed_hive_id"):
        origin = store.hive_or_none(mother["installed_hive_id"])
    queen = {
        "id": store.next_id("queens", "Q"),
        "emerged_date": emerged,
        "source": source,
        "origin_hive_id": origin["id"] if origin else None,
        "installed_hive_id": hive["id"],
        "mother_queen_id": mother["id"] if mother else None,
        "status": status,
        "notes": (args.notes or "").strip(),
        "created_at": now_iso(),
    }
    store.queens().append(queen)
    hive["current_queen_id"] = queen["id"]
    store.save()
    emit(
        f"Added queen {queen['id']} to {hive.get('label')} "
        f"(emerged {emerged}, {source}, {status})."
    )
    return EXIT_OK


def cmd_queen_list(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    queens = sorted(
        store.queens(),
        key=lambda row: (row.get("emerged_date", ""), _id_number(row.get("id"))),
        reverse=True,
    )
    if args.json:
        emit_json(queens)
        return EXIT_OK
    if not queens:
        emit(
            "No queens logged yet — add one with: "
            "hivesplit queen add --hive <id> --emerged <date> --source <source>"
        )
        return EXIT_OK
    rows = [queen_row(queen, store) for queen in queens]
    emit(render_table(["id", "hive", "emerged", "age", "source", "status"], rows))
    return EXIT_OK


def cmd_queen_show(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    queen = store.resolve_queen(args.queen)
    chain = dam_chain(store, queen)
    daughters = daughters_of(store, queen["id"])
    if args.json:
        payload = dict(queen)
        payload["dam_chain"] = [row["id"] for row in chain]
        payload["daughters"] = [row["id"] for row in daughters]
        emit_json(payload)
        return EXIT_OK

    emerged = queen.get("emerged_date", "")
    try:
        age = days_between(as_date(emerged), today())
        age_text = f"{age}d"
    except ValueError:
        age_text = EMPTY

    lines = [f"{queen.get('id')} — {queen.get('status')} queen ({queen.get('source')})"]
    fields = [
        ("Emerged", f"{emerged} ({age_text})"),
        ("Source", queen.get("source", "")),
        ("Status", queen.get("status", "")),
        ("Hive", store.hive_label(queen.get("installed_hive_id") or "")),
        ("Origin", store.hive_label(queen["origin_hive_id"]) if queen.get("origin_hive_id") else EMPTY),
    ]
    mother = store.queen_or_none(queen.get("mother_queen_id") or "")
    if mother:
        fields.append(
            ("Dam", f"{mother.get('id')} ({store.hive_label(mother.get('installed_hive_id') or '')})")
        )
    else:
        fields.append(("Dam", EMPTY))
    if len(chain) > 1:
        fields.append(("Dam line", " → ".join(row["id"] for row in chain)))
    if daughters:
        fields.append(("Daughters", ", ".join(row["id"] for row in daughters)))
    else:
        fields.append(("Daughters", EMPTY))
    if queen.get("notes"):
        fields.append(("Notes", queen["notes"]))
    lines.append(render_fields(fields))
    emit("\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: inspect
# --------------------------------------------------------------------------


def cmd_inspect_add(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    require_unlocked(store, session_file)
    hive = store.resolve_hive(args.hive)
    inspection_date = parse_date(args.date, "date").strftime(DATE_FORMAT)
    if any(
        row.get("hive_id") == hive["id"] and row.get("date") == inspection_date
        for row in store.inspections()
    ):
        raise HiveSplitError(
            f"{hive.get('label')} already has an inspection on {inspection_date}"
        )
    typed = {
        "queen_cells": parse_int(args.queen_cells, "queen-cells"),
        "queen_cups": parse_int(args.queen_cups, "queen-cups"),
        "brood_frames": parse_int(args.brood_frames, "brood-frames"),
        "bee_frames": parse_int(args.bee_frames, "bee-frames"),
        "crowding": parse_level(args.crowding, "crowding"),
        "stores": parse_level(args.stores, "stores"),
        "temperament": parse_level(args.temperament, "temperament"),
    }
    inspection = {
        "id": store.next_id("inspections", "I"),
        "hive_id": hive["id"],
        "date": inspection_date,
        **typed,
        "note": (args.note or "").strip(),
        "created_at": now_iso(),
    }
    queen = store.current_queen(hive)
    risk = compute_risk(inspection, queen)
    inspection["swarm_risk"] = risk["score"]
    inspection["risk_band"] = risk["band"]
    inspection["risk_factors"] = risk["factors"]
    store.inspections().append(inspection)
    store.save()
    emit(
        f"Recorded inspection {inspection['id']} for {hive.get('label')} "
        f"on {inspection_date} — swarm risk {risk['score']} ({risk['band']})."
    )
    return EXIT_OK


def cmd_inspect_list(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    hive = store.resolve_hive(args.hive)
    rows = store.inspections_for(hive["id"])
    if args.json:
        emit_json(rows)
        return EXIT_OK
    if not rows:
        emit(f"No inspections for {hive.get('label')} yet.")
        emit(
            f"Add one with: hivesplit inspect add --hive {hive.get('label')} "
            "--date YYYY-MM-DD --crowding 0"
        )
        return EXIT_OK
    table = render_table(
        ["date", "cells", "cups", "brood", "bees", "crowd", "stores", "temp", "risk", "band", "note"],
        [inspection_row(row) for row in rows],
        aligns=["left", "right", "right", "right", "right", "right", "right", "right", "right", "left", "left"],
    )
    emit(table)
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands: risk
# --------------------------------------------------------------------------


def _risk_for_hive(store: Store, hive: dict):
    latest = store.latest_inspection(hive["id"])
    if latest is None:
        return None
    queen = store.current_queen(hive)
    risk = compute_risk(latest, queen)
    return {"inspection": latest, "risk": risk, "queen": queen}


def cmd_risk(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)

    if args.hive:
        hive = store.resolve_hive(args.hive)
        report = _risk_for_hive(store, hive)
        if report is None:
            raise HiveSplitError(
                f"No inspections for {hive.get('label')} — risk needs at least one typed inspection"
            )
        if args.json:
            emit_json(
                {
                    "hive": hive["id"],
                    "label": hive.get("label"),
                    "date": report["inspection"]["date"],
                    "score": report["risk"]["score"],
                    "band": report["risk"]["band"],
                    "factors": report["risk"]["factors"],
                }
            )
            return EXIT_OK
        emit(_format_single_risk(store, hive, report))
        return EXIT_OK

    ranked = []
    for hive in store.hives():
        if hive.get("status") != "active":
            continue
        report = _risk_for_hive(store, hive)
        ranked.append((hive, report))
    if not ranked:
        if args.json:
            emit_json([])
        else:
            emit("No active hives yet")
        return EXIT_OK
    ranked.sort(
        key=lambda item: (
            -(item[1]["risk"]["score"] if item[1] else -1),
            _id_number(item[0].get("id")),
        )
    )
    if args.json:
        emit_json(
            [
                {
                    "hive": hive["id"],
                    "label": hive.get("label"),
                    "score": report["risk"]["score"] if report else None,
                    "band": report["risk"]["band"] if report else "unknown",
                    "date": report["inspection"]["date"] if report else None,
                }
                for hive, report in ranked
            ]
        )
        return EXIT_OK

    emit(f"Swarm risk — {store.apiary_name}, {plural(len(ranked), 'active hive')}")
    rows = [
        [
            hive.get("label", ""),
            hive.get("id", ""),
            report["risk"]["score"] if report else EMPTY,
            report["risk"]["band"] if report else "no data",
            report["inspection"]["date"] if report else "never inspected",
        ]
        for hive, report in ranked
    ]
    emit(
        render_table(
            ["hive", "id", "score", "band", "latest"],
            rows,
            aligns=["left", "left", "right", "left", "left"],
        )
    )
    return EXIT_OK


def _format_single_risk(store: Store, hive: dict, report: dict) -> str:
    risk = report["risk"]
    queen = report["queen"]
    inspection = report["inspection"]
    lines = [
        f"{hive.get('label')} — {hive.get('kind')} {hive.get('status')} ({hive.get('id')})",
        f"Latest inspection: {inspection['date']}",
        f"Swarm risk: {risk['score']} / {RISK_MAX} — {risk['band']}",
    ]
    lines.append(
        render_table(
            ["factor", "points", "why"],
            [
                [factor["label"], factor["points"], factor["detail"]]
                for factor in risk["factors"]
            ],
            aligns=["left", "right", "left"],
        )
    )
    lines.append(f"Total: {risk['score']} / {RISK_MAX} — {risk['band']}")
    if queen is None:
        lines.append("No current queen on record — queen age scored 0.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands: status
# --------------------------------------------------------------------------


def cmd_status(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    days = parse_int(args.days, "days")
    hives = store.hives()
    if args.json:
        payload = _status_payload(store, days)
        if payload is None:
            emit_json({"hives": 0})
            return EXIT_OK
        emit_json(payload)
        return EXIT_OK
    if not hives:
        emit("No hives yet")
        return EXIT_OK
    payload = _status_payload(store, days)
    emit(_format_status(store, payload, days))
    return EXIT_OK


def _status_payload(store: Store, days: int):
    hives = store.hives()
    if not hives:
        return None
    overdue = []
    never = []
    check_first = []
    for hive in hives:
        latest = store.latest_inspection(hive["id"])
        if latest is None:
            never.append({"hive": hive["id"], "label": hive.get("label")})
        else:
            age = days_between(as_date(latest["date"]), today())
            if age >= days:
                overdue.append(
                    {
                        "hive": hive["id"],
                        "label": hive.get("label"),
                        "date": latest["date"],
                        "days": age,
                    }
                )
        if hive.get("status") == "active":
            report = _risk_for_hive(store, hive)
            check_first.append(
                {
                    "hive": hive["id"],
                    "label": hive.get("label"),
                    "score": report["risk"]["score"] if report else None,
                    "band": report["risk"]["band"] if report else "no data",
                }
            )
    check_first.sort(key=lambda row: (-(row["score"] if row["score"] is not None else -1), _id_number(row["hive"])))
    overdue.sort(key=lambda row: (-row["days"], _id_number(row["hive"])))
    return {
        "apiary": store.apiary_name,
        "hives": len(hives),
        "queens": len(store.queens()),
        "inspections": len(store.inspections()),
        "days": days,
        "overdue": overdue,
        "never_inspected": never,
        "check_first": check_first,
    }


def _format_status(store: Store, payload: dict, days: int) -> str:
    lines = [
        f"{payload['apiary']} — {plural(payload['hives'], 'hive')}, "
        f"{plural(payload['queens'], 'queen')}, "
        f"{plural(payload['inspections'], 'inspection')}"
    ]

    lines.append("")
    lines.append("Check first (highest swarm risk)")
    if payload["check_first"]:
        rows = [
            [
                row["label"],
                row["hive"],
                row["score"] if row["score"] is not None else EMPTY,
                row["band"],
            ]
            for row in payload["check_first"]
        ]
        lines.append(
            render_table(["hive", "id", "score", "band"], rows, aligns=["left", "left", "right", "left"])
        )
    else:
        lines.append("  No active hives")

    lines.append("")
    lines.append(f"Not inspected in {days}+ days")
    if payload["overdue"]:
        rows = [
            [row["label"], row["hive"], f"{row['days']}d", row["date"]]
            for row in payload["overdue"]
        ]
        lines.append(
            render_table(["hive", "id", "days", "last"], rows, aligns=["left", "left", "right", "left"])
        )
    else:
        lines.append(f"  None — nothing is overdue at {days} days")

    lines.append("")
    lines.append("Never inspected")
    if payload["never_inspected"]:
        rows = [[row["label"], row["hive"]] for row in payload["never_inspected"]]
        lines.append(render_table(["hive", "id"], rows))
    else:
        lines.append("  None — every hive has at least one inspection")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands: export
# --------------------------------------------------------------------------

CSV_FIELDS = [
    "id",
    "label",
    "kind",
    "yard",
    "established_date",
    "status",
    "current_queen_id",
    "parent_hive_id",
    "parent_split_date",
    "last_inspection_date",
]


def cmd_export(args, store_path: Path, session_file: Path) -> int:
    store = Store.open(store_path)
    require_unlocked(store, session_file)
    export_format = (args.format or "").strip().lower()
    if export_format not in ("json", "csv"):
        raise HiveSplitError("format must be one of: json, csv")

    if export_format == "json":
        payload = {
            "apiary": store.apiary_name,
            "exported_at": now_iso(),
            "version": store.data.get("version", SCHEMA_VERSION),
            "hives": store.hives(),
            "splits": store.splits(),
            "queens": store.queens(),
            "inspections": store.inspections(),
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        row_count = len(store.hives())
    else:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for hive in sorted(store.hives(), key=lambda row: _id_number(row.get("id"))):
            parent = store.parent_split(hive["id"])
            latest = store.latest_inspection(hive["id"])
            writer.writerow(
                {
                    "id": hive.get("id", ""),
                    "label": hive.get("label", ""),
                    "kind": hive.get("kind", ""),
                    "yard": hive.get("yard", ""),
                    "established_date": hive.get("established_date", ""),
                    "status": hive.get("status", ""),
                    "current_queen_id": hive.get("current_queen_id") or "",
                    "parent_hive_id": store.hive_label(parent["parent_hive_id"]) if parent else "",
                    "parent_split_date": parent.get("date", "") if parent else "",
                    "last_inspection_date": latest.get("date", "") if latest else "",
                }
            )
        text = buffer.getvalue()
        row_count = len(store.hives())

    if args.out:
        target = Path(args.out).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise HiveSplitError(
                f"Cannot write {target}: {exc.strerror or exc}"
            ) from None
        print(
            f"Wrote {plural(row_count, 'hive')} as {export_format.upper()} to {target}",
            file=sys.stderr,
        )
        return EXIT_OK

    sys.stdout.write(text)
    return EXIT_OK


# --------------------------------------------------------------------------
# argparse wiring
# --------------------------------------------------------------------------


def add_shared_options(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--store",
        default=default,
        metavar="PATH",
        help=f"path to the JSON store (default ./{DEFAULT_STORE})",
    )
    parser.add_argument(
        "--session",
        default=default,
        metavar="PATH",
        help=f"path to the session file (default ./{SESSION_FILENAME} beside the store)",
    )


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON on stdout"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivesplit",
        description=(
            "HiveSplit — apiary hive split genealogy tracker. "
            "Hives, splits, queens and typed inspections in one local JSON file."
        ),
        epilog=(
            "examples:\n"
            '  hivesplit init --apiary "Willow Yard"\n'
            "  hivesplit unlock\n"
            "  hivesplit hive add --label H1 --kind production --yard North\n"
            "  hivesplit split --from H1 --label H2 --date 2025-05-10 --method walk-away\n"
            "  hivesplit inspect add --hive H2 --date 2025-06-01 --queen-cells 3 --crowding 2\n"
            "  hivesplit risk\n"
            "  hivesplit status --days 14\n"
            "  hivesplit export --format csv --out hives.csv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"hivesplit {__version__}")
    add_shared_options(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init = subparsers.add_parser("init", help="create the store and set the keeper passphrase")
    init.add_argument("--apiary", required=True, help='apiary name, e.g. "Willow Yard"')
    add_shared_options(init, suppress=True)
    init.set_defaults(handler=cmd_init)

    unlock = subparsers.add_parser("unlock", help="open a 12-hour session so writes are allowed")
    add_shared_options(unlock, suppress=True)
    unlock.set_defaults(handler=cmd_unlock)

    hive = subparsers.add_parser("hive", help="add, list, show or retire a hive")
    add_shared_options(hive, suppress=True)
    hive_sub = hive.add_subparsers(dest="hive_command", metavar="<action>")

    hive_add = hive_sub.add_parser("add", help="add a hive to the apiary")
    hive_add.add_argument("--label", required=True, help="unique hive label, e.g. H1")
    hive_add.add_argument("--kind", default="production", help=", ".join(HIVE_KINDS))
    hive_add.add_argument("--yard", default="", help="free-text yard name")
    hive_add.add_argument("--established", default="", help="established date YYYY-MM-DD (default today)")
    hive_add.add_argument("--notes", default="", help="free-text note")
    add_shared_options(hive_add, suppress=True)
    hive_add.set_defaults(handler=cmd_hive_add)

    hive_list = hive_sub.add_parser("list", help="list every hive as a table")
    add_json_flag(hive_list)
    add_shared_options(hive_list, suppress=True)
    hive_list.set_defaults(handler=cmd_hive_list)

    hive_show = hive_sub.add_parser("show", help="show one hive's detail")
    hive_show.add_argument("hive", help="hive id or label")
    add_json_flag(hive_show)
    add_shared_options(hive_show, suppress=True)
    hive_show.set_defaults(handler=cmd_hive_show)

    hive_status = hive_sub.add_parser("status", help="change a hive's status")
    hive_status.add_argument("hive", help="hive id or label")
    hive_status.add_argument("status", help=", ".join(HIVE_STATUSES))
    add_shared_options(hive_status, suppress=True)
    hive_status.set_defaults(handler=cmd_hive_status)

    split = subparsers.add_parser("split", help="record a split: new child hive plus its edge")
    split.add_argument("--from", dest="parent", required=True, help="parent hive id or label")
    split.add_argument("--label", required=True, help="label for the new child hive")
    split.add_argument("--date", required=True, help="split date, YYYY-MM-DD")
    split.add_argument("--method", default="walk-away", help=", ".join(SPLIT_METHODS))
    split.add_argument("--kind", default="split", help="child hive kind: " + ", ".join(HIVE_KINDS))
    split.add_argument("--yard", default="", help="child yard (inherits the parent's by default)")
    split.add_argument("--queen", default="", help="queen id to install in the new hive")
    split.add_argument("--notes", default="", help="free-text note")
    add_shared_options(split, suppress=True)
    split.set_defaults(handler=cmd_split)

    tree = subparsers.add_parser("tree", help="render the apiary lineage as an indented tree")
    tree.add_argument("--hive", default="", help="render only this hive's subtree")
    add_json_flag(tree)
    add_shared_options(tree, suppress=True)
    tree.set_defaults(handler=cmd_tree)

    queen = subparsers.add_parser("queen", help="add, list or show queens")
    add_shared_options(queen, suppress=True)
    queen_sub = queen.add_subparsers(dest="queen_command", metavar="<action>")

    queen_add = queen_sub.add_parser("add", help="record a queen")
    queen_add.add_argument("--hive", required=True, help="hive the queen is installed in")
    queen_add.add_argument("--emerged", required=True, help="emergence (production) date, YYYY-MM-DD")
    queen_add.add_argument("--source", required=True, help=", ".join(QUEEN_SOURCES))
    queen_add.add_argument("--origin", default="", help="hive she was produced in")
    queen_add.add_argument("--mother", default="", help="mother queen id (dam line)")
    queen_add.add_argument("--status", default="laying", help=", ".join(QUEEN_STATUSES))
    queen_add.add_argument("--notes", default="", help="free-text note")
    add_shared_options(queen_add, suppress=True)
    queen_add.set_defaults(handler=cmd_queen_add)

    queen_list = queen_sub.add_parser("list", help="list queens newest-first with age in days")
    add_json_flag(queen_list)
    add_shared_options(queen_list, suppress=True)
    queen_list.set_defaults(handler=cmd_queen_list)

    queen_show = queen_sub.add_parser("show", help="show one queen with her dam chain and daughters")
    queen_show.add_argument("queen", help="queen id")
    add_json_flag(queen_show)
    add_shared_options(queen_show, suppress=True)
    queen_show.set_defaults(handler=cmd_queen_show)

    inspect = subparsers.add_parser("inspect", help="record or list typed inspections")
    add_shared_options(inspect, suppress=True)
    inspect_sub = inspect.add_subparsers(dest="inspect_command", metavar="<action>")

    inspect_add = inspect_sub.add_parser("add", help="record one typed inspection")
    inspect_add.add_argument("--hive", required=True, help="hive id or label")
    inspect_add.add_argument("--date", required=True, help="inspection date, YYYY-MM-DD")
    inspect_add.add_argument("--queen-cells", dest="queen_cells", default="0", help="queen cells seen (default 0)")
    inspect_add.add_argument("--queen-cups", dest="queen_cups", default="0", help="queen cups seen (default 0)")
    inspect_add.add_argument("--brood-frames", dest="brood_frames", default="0", help="brood frames (default 0)")
    inspect_add.add_argument("--bee-frames", dest="bee_frames", default="0", help="adult bee frames (default 0)")
    inspect_add.add_argument("--crowding", default="0", help="crowding 0-3 (default 0)")
    inspect_add.add_argument("--stores", default="0", help="stores 0-3 (default 0)")
    inspect_add.add_argument("--temperament", default="0", help="temperament 0-3 (default 0)")
    inspect_add.add_argument("--note", default="", help="free-text note")
    add_shared_options(inspect_add, suppress=True)
    inspect_add.set_defaults(handler=cmd_inspect_add)

    inspect_list = inspect_sub.add_parser("list", help="list one hive's inspections newest-first")
    inspect_list.add_argument("--hive", required=True, help="hive id or label")
    add_json_flag(inspect_list)
    add_shared_options(inspect_list, suppress=True)
    inspect_list.set_defaults(handler=cmd_inspect_list)

    risk = subparsers.add_parser("risk", help="swarm-risk score, band and factor breakdown")
    risk.add_argument("--hive", default="", help="hive id or label; omit to rank every active hive")
    add_json_flag(risk)
    add_shared_options(risk, suppress=True)
    risk.set_defaults(handler=cmd_risk)

    status = subparsers.add_parser("status", help="apiary briefing: overdue, never inspected, check first")
    status.add_argument("--days", default="14", help="overdue threshold in days (default 14, 0 lists every inspected hive)")
    add_json_flag(status)
    add_shared_options(status, suppress=True)
    status.set_defaults(handler=cmd_status)

    export = subparsers.add_parser("export", help="export json or hive csv to stdout or --out")
    export.add_argument("--format", required=True, help="json or csv")
    export.add_argument("--out", default="", help="write to this file instead of stdout")
    add_shared_options(export, suppress=True)
    export.set_defaults(handler=cmd_export)

    return parser


WELCOME = """HiveSplit {version} — apiary hive split genealogy tracker

Hives, splits, queens and typed inspections in one local JSON file.

  hivesplit init --apiary "Willow Yard"     create the store, set the passphrase
  hivesplit unlock                          open a 12-hour write session
  hivesplit hive add|list|show|status       the apiary roster
  hivesplit split --from H1 --label H2 ...  record a split, child hive + edge
  hivesplit tree [--hive H2]                lineage as an indented tree
  hivesplit queen add|list|show             queen production log and dam lines
  hivesplit inspect add|list                typed inspection notes
  hivesplit risk [--hive H2]                deterministic swarm-risk score
  hivesplit status [--days 14]              overdue / never-inspected / check first
  hivesplit export --format json|csv        back the data up

Read commands work while locked; writes and export need `hivesplit unlock`.
Run `hivesplit --help` for the full option list, or `bash demo.sh` for a tour.
"""


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stdout.write(WELCOME.format(version=__version__))
        return EXIT_OK

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE

    store_path = Path(args.store or DEFAULT_STORE).expanduser()
    session_file = session_path_for(store_path, getattr(args, "session", None))

    try:
        return handler(args, store_path, session_file)
    except HiveSplitError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, StoreError) else EXIT_USER
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return EXIT_USER


if __name__ == "__main__":
    sys.exit(main())
