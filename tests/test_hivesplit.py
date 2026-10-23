"""End-to-end tests for the HiveSplit CLI.

Every test drives the real entry point (`python3 hivesplit.py ...`) except the
two in-process tests that need to patch the filesystem, and asserts the
documented exit-code contract: 0 success, 1 domain error, 2 usage or store
error.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hivesplit.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class Result:
    def __init__(self, completed: subprocess.CompletedProcess) -> None:
        self.code = completed.returncode
        self.out = completed.stdout
        self.err = completed.stderr

    @property
    def both(self) -> str:
        return self.out + self.err


def hivesplit(store: Path, *args: str, passphrase: str = "cenius", stdin: str = "") -> Result:
    env = os.environ.copy()
    env.pop("HIVESPLIT_PASSPHRASE", None)
    if passphrase is not None:
        env["HIVESPLIT_PASSPHRASE"] = passphrase
    command = [sys.executable, str(SCRIPT), *args, "--store", str(store)]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        input=stdin,
        cwd=str(store.parent),
        timeout=60,
    )
    assert "Traceback" not in completed.stderr, completed.stderr
    return Result(completed)


def ok(store: Path, *args: str, **kwargs) -> Result:
    result = hivesplit(store, *args, **kwargs)
    assert result.code == 0, f"{args} failed ({result.code}): {result.both}"
    return result


def days_ago(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def read_store(store: Path) -> dict:
    return json.loads(store.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "hivesplit.json"


@pytest.fixture
def apiary(store: Path) -> Path:
    """An unlocked apiary with two base hives."""
    ok(store, "init", "--apiary", "Willow Yard Apiary")
    ok(store, "unlock")
    ok(store, "hive", "add", "--label", "H1", "--kind", "production", "--yard", "North",
       "--established", "2024-04-01")
    ok(store, "hive", "add", "--label", "H2", "--kind", "production", "--yard", "North",
       "--established", "2024-04-02")
    return store


# --------------------------------------------------------------------------
# T1 — skeleton, dispatch, store errors
# --------------------------------------------------------------------------


def test_help_lists_every_command(store):
    result = hivesplit(store, "--help")
    assert result.code == 0
    for command in ("init", "unlock", "hive", "split", "tree", "queen", "inspect", "risk", "status", "export"):
        assert command in result.out


def test_bare_invocation_prints_usage_and_exits_zero(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True,
        cwd=str(tmp_path), timeout=60,
    )
    assert completed.returncode == 0
    assert "HiveSplit" in completed.stdout
    assert "hivesplit init" in completed.stdout


def test_version_flag(store):
    result = hivesplit(store, "--version")
    assert result.code == 0
    assert result.out.strip().startswith("hivesplit ")


def test_corrupt_store_exits_two_with_one_line(store):
    store.write_text("{not json", encoding="utf-8")
    result = hivesplit(store, "hive", "list")
    assert result.code == 2
    assert "Cannot read store" in result.err
    assert len([line for line in result.both.splitlines() if line.strip()]) == 1


def test_missing_store_exits_two(tmp_path):
    missing = tmp_path / "apiary.json"
    result = hivesplit(missing, "hive", "list")
    assert result.code == 2
    assert f"No store at {missing}" in result.err
    assert "run hivesplit init" in result.err


def test_store_flag_accepted_before_the_command(tmp_path):
    target = tmp_path / "before.json"
    env = os.environ.copy()
    env["HIVESPLIT_PASSPHRASE"] = "cenius"
    subprocess.run([sys.executable, str(SCRIPT), "--store", str(target), "init", "--apiary", "Willow"],
                   capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60, check=True)
    assert target.exists()


def test_atomic_save_leaves_the_original_json_intact(tmp_path, monkeypatch):
    import hivesplit as module

    path = tmp_path / "store.json"
    keeper = {
        "keeper_id": "K1",
        "apiary_name": "Willow Yard",
        "passphrase_hash": "00",
        "salt": "00" * 16,
        "iterations": 1000,
        "created_at": "2025-01-01T00:00:00",
    }
    instance = module.Store.create(path, keeper)
    instance.save()
    original = path.read_bytes()
    instance.data["hives"].append({"id": "H1", "label": "H1"})

    def fail_replace(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(module.StoreError):
        instance.save()
    monkeypatch.undo()

    assert path.read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["hives"] == []
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["store.json"]


# --------------------------------------------------------------------------
# T2 — init, passphrase hash, session, locked refusal
# --------------------------------------------------------------------------


def test_init_writes_hashed_passphrase_and_apiary_name(store):
    result = ok(store, "init", "--apiary", "Willow Yard")
    assert "Willow Yard" in result.out
    document = read_store(store)
    keeper = document["keeper"]
    assert keeper["apiary_name"] == "Willow Yard"
    assert len(keeper["passphrase_hash"]) == 64
    assert len(keeper["salt"]) == 32
    assert keeper["iterations"] >= 100_000
    text = store.read_text(encoding="utf-8")
    assert "cenius" not in text
    assert "passphrase" not in json.dumps(keeper).replace("passphrase_hash", "")


def test_init_refuses_to_overwrite(store):
    ok(store, "init", "--apiary", "Willow Yard")
    before = store.read_bytes()
    result = hivesplit(store, "init", "--apiary", "Other Yard")
    assert result.code == 1
    assert f"Store already exists at {store}" in result.err
    assert store.read_bytes() == before


def test_init_rejects_mismatched_passphrases(store):
    result = hivesplit(store, "init", "--apiary", "Willow", passphrase=None, stdin="one\ntwo\n")
    assert result.code == 1
    assert "Passphrases do not match" in result.err
    assert not store.exists()


def test_init_rejects_short_passphrase(store):
    result = hivesplit(store, "init", "--apiary", "Willow", passphrase="ab")
    assert result.code == 1
    assert "at least" in result.err


def test_unlock_writes_twelve_hour_session(store):
    ok(store, "init", "--apiary", "Willow Yard")
    result = ok(store, "unlock")
    assert "Unlocked until" in result.out
    session = store.parent / ".hivesplit.session"
    assert session.exists()
    payload = json.loads(session.read_text(encoding="utf-8"))
    expires = dt.datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%S")
    remaining = (expires - dt.datetime.now()).total_seconds()
    assert 11 * 3600 < remaining <= 12 * 3600 + 60


def test_unlock_with_wrong_passphrase_is_non_enumerating(store):
    ok(store, "init", "--apiary", "Willow Yard")
    result = hivesplit(store, "unlock", passphrase="wrong")
    assert result.code == 1
    assert result.both.strip() == "Passphrase does not match"


def test_locked_write_is_refused_and_writes_nothing(store):
    ok(store, "init", "--apiary", "Willow Yard")
    before = store.read_bytes()
    result = hivesplit(store, "hive", "add", "--label", "H1")
    assert result.code == 1
    assert "Locked — run hivesplit unlock" in result.err
    assert store.read_bytes() == before
    assert read_store(store)["hives"] == []


def test_passphrase_hash_is_pbkdf2_hmac_sha256():
    import hivesplit as module

    salt = "ab" * 16
    digest = module.hash_passphrase("cenius", salt, 2000)
    expected = hashlib.pbkdf2_hmac("sha256", b"cenius", bytes.fromhex(salt), 2000).hex()
    assert digest == expected


# --------------------------------------------------------------------------
# T3 — hive registry
# --------------------------------------------------------------------------


def test_hive_add_then_list_shows_the_row(apiary):
    result = ok(apiary, "hive", "add", "--label", "H3", "--kind", "nuc", "--yard", "South")
    assert "H3" in result.out
    listing = ok(apiary, "hive", "list").out
    row = next(line for line in listing.splitlines() if line.startswith("H3"))
    assert "nuc" in row
    assert "South" in row
    assert "active" in row


def test_hive_add_rejects_duplicate_label(apiary):
    before = apiary.read_bytes()
    result = hivesplit(apiary, "hive", "add", "--label", "H1")
    assert result.code == 1
    assert "A hive labelled H1 already exists" in result.err
    assert apiary.read_bytes() == before


def test_hive_list_empty_prints_the_exact_hint(store):
    ok(store, "init", "--apiary", "Willow Yard")
    result = ok(store, "hive", "list")
    assert result.out.strip() == "No hives yet — add one with: hivesplit hive add --label <name>"


def test_hive_show_without_inspections(apiary):
    result = ok(apiary, "hive", "show", "H1")
    assert "H1" in result.out
    assert "production" in result.out
    assert "North" in result.out
    assert "2024-04-01" in result.out
    assert "active" in result.out
    assert "No inspections yet" in result.out


def test_hive_status_change_keeps_history(apiary):
    ok(apiary, "split", "--from", "H1", "--label", "H3", "--date", "2025-05-10",
       "--method", "walk-away")
    result = ok(apiary, "hive", "status", "H1", "merged")
    assert "merged" in result.out
    listing = ok(apiary, "hive", "list").out
    row = next(line for line in listing.splitlines() if line.startswith("H1"))
    assert "merged" in row
    document = read_store(apiary)
    assert len(document["splits"]) == 1
    assert document["hives"][0]["established_date"] == "2024-04-01"


def test_hive_show_unknown_id(apiary):
    result = hivesplit(apiary, "hive", "show", "H9")
    assert result.code == 1
    assert "No hive H9" in result.err


# --------------------------------------------------------------------------
# T4 — splits and the lineage tree
# --------------------------------------------------------------------------


def test_split_creates_child_and_tree_shows_date(apiary):
    result = ok(apiary, "split", "--from", "H1", "--label", "H3", "--date", "2025-05-10",
                "--method", "walk-away")
    assert "H3" in result.out
    tree = ok(apiary, "tree").out
    lines = tree.splitlines()
    root = next(line for line in lines if line.startswith("H1 ["))
    child = next(line for line in lines if line.strip().startswith("H3 ["))
    assert child.startswith("  ") and not child.startswith("    ")
    assert "2025-05-10" in child
    assert "walk-away" in child
    assert root.startswith("H1 [")


def test_tree_nests_three_generations_and_subtree_view(apiary):
    ok(apiary, "split", "--from", "H1", "--label", "H3", "--date", "2025-05-10",
       "--method", "walk-away")
    ok(apiary, "split", "--from", "H3", "--label", "H4", "--date", "2025-06-02",
       "--method", "cut-down", "--kind", "nuc")
    lines = ok(apiary, "tree").out.splitlines()
    depths = {}
    for line in lines:
        label = line.strip().split(" ")[0]
        depths[label] = (len(line) - len(line.lstrip(" "))) // 2
    assert depths["H1"] == 0
    assert depths["H3"] == 1
    assert depths["H4"] == 2
    assert "H2" in ok(apiary, "tree").out

    subtree = ok(apiary, "tree", "--hive", "H3").out
    assert "H3 [" in subtree
    assert "H4 [" in subtree
    assert "H1 [" not in subtree
    assert "H2 [" not in subtree


def test_tree_without_splits_prints_hint(apiary):
    result = ok(apiary, "tree")
    assert "No splits recorded yet — record one with: hivesplit split" in result.out


def test_hive_show_lists_children_with_split_date(apiary):
    ok(apiary, "split", "--from", "H1", "--label", "H3", "--date", "2025-05-10",
       "--method", "walk-away")
    result = ok(apiary, "hive", "show", "H1")
    children = next(line for line in result.out.splitlines() if line.startswith("Children"))
    assert "H3 (2025-05-10)" in children


def test_split_with_unknown_parent_writes_nothing(apiary):
    before = apiary.read_bytes()
    result = hivesplit(apiary, "split", "--from", "H9", "--label", "H7",
                       "--date", "2025-05-10", "--method", "walk-away")
    assert result.code == 1
    assert "No hive H9" in result.err
    assert apiary.read_bytes() == before
    assert not any(hive["label"] == "H7" for hive in read_store(apiary)["hives"])


def test_split_attaches_a_queen(apiary):
    ok(apiary, "queen", "add", "--hive", "H1", "--emerged", "2025-04-01", "--source", "grafted")
    ok(apiary, "split", "--from", "H1", "--label", "H3", "--date", "2025-05-10",
       "--method", "grafted-queen", "--queen", "Q1")
    result = ok(apiary, "hive", "show", "H3")
    assert "Queen          : Q1 (laying, emerged 2025-04-01)" in result.out
    document = read_store(apiary)
    queen = next(queen for queen in document["queens"] if queen["id"] == "Q1")
    assert queen["installed_hive_id"] == "H3"
    assert document["splits"][0]["queen_id"] == "Q1"


def test_split_rejects_bad_method(apiary):
    before = apiary.read_bytes()
    result = hivesplit(apiary, "split", "--from", "H1", "--label", "H3",
                       "--date", "2025-05-10", "--method", "teleport")
    assert result.code == 1
    assert "method must be one of" in result.err
    assert apiary.read_bytes() == before


# --------------------------------------------------------------------------
# T5 — queen log
# --------------------------------------------------------------------------


def test_queen_add_then_list_shows_age(apiary):
    emerged = days_ago(120)
    ok(apiary, "queen", "add", "--hive", "H2", "--emerged", emerged, "--source", "grafted")
    listing = ok(apiary, "queen", "list").out
    row = next(line for line in listing.splitlines() if line.startswith("Q1"))
    assert "H2" in row
    assert emerged in row
    assert "120d" in row
    assert "grafted" in row
    assert "laying" in row


def test_hive_show_prints_the_current_queen(apiary):
    ok(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-08", "--source", "grafted")
    result = ok(apiary, "hive", "show", "H2")
    assert "Queen          : Q1 (laying, emerged 2025-05-08)" in result.out


def test_queen_add_rejects_unknown_source(apiary):
    result = hivesplit(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-08",
                       "--source", "mail-order")
    assert result.code == 1
    for source in ("grafted", "swarm-cell", "supersedure-cell", "purchased", "unknown"):
        assert source in result.err
    assert read_store(apiary)["queens"] == []


def test_queen_list_empty_prints_the_hint(apiary):
    result = ok(apiary, "queen", "list")
    assert (
        "No queens logged yet — add one with: "
        "hivesplit queen add --hive <id> --emerged <date> --source <source>"
    ) in result.out


# --------------------------------------------------------------------------
# T6 — typed inspections
# --------------------------------------------------------------------------


def inspect_h2(store, date="2025-06-01", crowding="2"):
    return ok(
        store, "inspect", "add", "--hive", "H2", "--date", date,
        "--queen-cells", "3", "--queen-cups", "2", "--brood-frames", "6",
        "--bee-frames", "9", "--crowding", crowding, "--stores", "2",
        "--temperament", "1", "--note", "bearding at entrance",
    )


def test_inspect_add_then_list(apiary):
    inspect_h2(apiary)
    listing = ok(apiary, "inspect", "list", "--hive", "H2").out
    row = next(line for line in listing.splitlines() if line.startswith("2025-06-01"))
    assert row.split()[:8] == ["2025-06-01", "3", "2", "6", "9", "2", "2", "1"]
    assert "bearding at entrance" in row


def test_inspect_rejects_second_inspection_same_date(apiary):
    inspect_h2(apiary)
    before = apiary.read_bytes()
    result = hivesplit(apiary, "inspect", "add", "--hive", "H2", "--date", "2025-06-01",
                       "--crowding", "1")
    assert result.code == 1
    assert "H2 already has an inspection on 2025-06-01" in result.err
    assert apiary.read_bytes() == before
    assert len(read_store(apiary)["inspections"]) == 1


def test_inspect_rejects_out_of_range_level(apiary):
    before = apiary.read_bytes()
    result = hivesplit(apiary, "inspect", "add", "--hive", "H2", "--date", "2025-06-02",
                       "--crowding", "7")
    assert result.code == 1
    assert "crowding must be 0-3" in result.err
    assert apiary.read_bytes() == before


def test_inspect_rejects_impossible_date(apiary):
    result = hivesplit(apiary, "inspect", "add", "--hive", "H2", "--date", "2025-13-40")
    assert result.code == 1
    assert "must be a real date in YYYY-MM-DD form" in result.err
    assert read_store(apiary)["inspections"] == []


def test_inspect_list_without_records(apiary):
    result = ok(apiary, "inspect", "list", "--hive", "H2")
    assert result.out.splitlines()[0] == "No inspections for H2 yet."


# --------------------------------------------------------------------------
# T7 — swarm risk
# --------------------------------------------------------------------------


def test_risk_single_hive_is_deterministic_with_factors(apiary):
    ok(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-08", "--source", "grafted")
    inspect_h2(apiary)
    first = ok(apiary, "risk", "--hive", "H2").out
    second = ok(apiary, "risk", "--hive", "H2").out
    assert first == second
    score_line = next(line for line in first.splitlines() if line.startswith("Swarm risk:"))
    score = int(score_line.split()[2])
    assert 0 <= score <= 100
    assert any(band in score_line for band in ("low", "elevated", "high"))
    for factor in ("queen cells", "queen cups", "crowding", "brood pressure", "queen age"):
        assert factor in first


def test_risk_reports_no_data_for_uninspected_hive(apiary):
    result = hivesplit(apiary, "risk", "--hive", "H1")
    assert result.code == 1
    assert "No inspections for H1 — risk needs at least one typed inspection" in result.err


def test_risk_ranking_is_ordered_highest_first(apiary):
    ok(apiary, "hive", "add", "--label", "H3")
    inspect_h2(apiary, date="2025-06-01", crowding="1")
    ok(apiary, "inspect", "add", "--hive", "H1", "--date", "2025-06-01",
       "--queen-cells", "5", "--queen-cups", "4", "--brood-frames", "12",
       "--bee-frames", "4", "--crowding", "3", "--stores", "1", "--temperament", "2")
    payload = json.loads(ok(apiary, "risk", "--json").out)
    scores = [row["score"] for row in payload if row["score"] is not None]
    assert scores == sorted(scores, reverse=True)
    assert payload[0]["label"] == "H1"
    assert payload[0]["band"] == "high"
    assert "HIVE" in ok(apiary, "risk").out


def test_inspect_list_shows_the_stored_snapshot(apiary):
    inspect_h2(apiary)
    row = next(
        line for line in ok(apiary, "inspect", "list", "--hive", "H2").out.splitlines()
        if line.startswith("2025-06-01")
    )
    snapshot = read_store(apiary)["inspections"][0]
    assert str(snapshot["swarm_risk"]) in row
    assert snapshot["risk_band"] in row
    assert snapshot["risk_factors"]


def test_risk_formula_caps_at_one_hundred():
    import hivesplit as module

    inspection = {
        "date": "2025-06-01",
        "queen_cells": 40,
        "queen_cups": 40,
        "brood_frames": 40,
        "bee_frames": 0,
        "crowding": 3,
        "stores": 0,
        "temperament": 0,
    }
    queen = {"id": "Q1", "emerged_date": "2020-01-01"}
    first = module.compute_risk(inspection, queen)
    second = module.compute_risk(dict(inspection), dict(queen))
    assert first == second
    assert first["score"] == module.RISK_MAX
    assert first["band"] == "high"
    assert sum(factor["points"] for factor in first["factors"]) >= module.RISK_MAX


# --------------------------------------------------------------------------
# T8 — queen dam lines
# --------------------------------------------------------------------------


def test_queen_dam_chain_and_daughters(apiary):
    ok(apiary, "queen", "add", "--hive", "H1", "--emerged", "2025-04-01", "--source", "grafted")
    ok(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-01",
       "--source", "supersedure-cell", "--mother", "Q1")
    ok(apiary, "hive", "add", "--label", "H3")
    ok(apiary, "queen", "add", "--hive", "H3", "--emerged", "2025-06-01",
       "--source", "grafted", "--mother", "Q2")

    daughters = ok(apiary, "queen", "show", "Q1").out
    assert "Daughters: Q2" in daughters

    child = ok(apiary, "queen", "show", "Q2").out
    assert "Dam      : Q1 (H1)" in child

    grandchild = ok(apiary, "queen", "show", "Q3").out
    dam_line = next(line for line in grandchild.splitlines() if line.startswith("Dam line"))
    assert "Q1 → Q2 → Q3" in dam_line
    assert "Dam      : Q2 (H2)" in grandchild


def test_queen_add_rejects_unknown_mother(apiary):
    before = apiary.read_bytes()
    result = hivesplit(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-08",
                       "--source", "grafted", "--mother", "Q9")
    assert result.code == 1
    assert "No queen Q9" in result.err
    assert apiary.read_bytes() == before


# --------------------------------------------------------------------------
# T9 — validation contract
# --------------------------------------------------------------------------


def test_empty_label_is_rejected_and_store_is_untouched(apiary):
    before = apiary.read_bytes()
    result = hivesplit(apiary, "hive", "add", "--label", "")
    assert result.code == 1
    assert "label cannot be empty" in result.err
    assert apiary.read_bytes() == before


def test_impossible_established_date_is_rejected(apiary):
    result = hivesplit(apiary, "hive", "add", "--label", "H5", "--established", "2024-02-30")
    assert result.code == 1
    assert "2024-02-30 is not a real date" in result.err
    assert not any(hive["label"] == "H5" for hive in read_store(apiary)["hives"])


def test_unknown_kind_lists_the_allowed_values(apiary):
    result = hivesplit(apiary, "hive", "add", "--label", "H5", "--kind", "langstroth")
    assert result.code == 1
    assert result.err.strip() == "kind must be one of: production, nuc, split"


def test_missing_required_argument_exits_two(apiary):
    result = hivesplit(apiary, "hive", "add")
    assert result.code == 2
    assert "usage: hivesplit hive add" in result.err


@pytest.mark.parametrize(
    "args",
    [
        ("hive", "add", "--label", ""),
        ("hive", "add", "--label", "H5", "--established", "2024-02-30"),
        ("hive", "add", "--label", "H5", "--kind", "langstroth"),
        ("hive", "show", "H9"),
        ("hive", "status", "H1", "retired"),
        ("split", "--from", "H9", "--label", "H7", "--date", "2025-05-10", "--method", "walk-away"),
        ("split", "--from", "H1", "--label", "H7", "--date", "2025-05-10", "--method", "nope"),
        ("queen", "add", "--hive", "H9", "--emerged", "2025-05-08", "--source", "grafted"),
        ("queen", "add", "--hive", "H1", "--emerged", "nope", "--source", "grafted"),
        ("queen", "add", "--hive", "H1", "--emerged", "2025-05-08", "--source", "nope"),
        ("queen", "show", "Q9"),
        ("inspect", "add", "--hive", "H1", "--date", "2025-06-01", "--crowding", "9"),
        ("inspect", "add", "--hive", "H1", "--date", "2025-06-01", "--queen-cells", "-2"),
        ("inspect", "add", "--hive", "H1", "--date", "06/01/2025"),
        ("status", "--days", "abc"),
        ("status", "--days", "-3"),
        ("risk", "--hive", "H1"),
        ("export", "--format", "xml"),
    ],
)
def test_every_bad_input_is_one_plain_line(apiary, args):
    result = hivesplit(apiary, *args)
    assert result.code == 1, result.both
    assert len([line for line in result.both.splitlines() if line.strip()]) == 1


# --------------------------------------------------------------------------
# T10 — export
# --------------------------------------------------------------------------


def populated(store: Path) -> Path:
    ok(store, "split", "--from", "H1", "--label", "H3", "--date", "2025-05-10",
       "--method", "walk-away")
    ok(store, "queen", "add", "--hive", "H3", "--emerged", "2025-05-10", "--source", "grafted")
    inspect_h2(store)
    return store


def test_export_json_has_all_four_collections(apiary):
    populated(apiary)
    result = ok(apiary, "export", "--format", "json")
    payload = json.loads(result.out)
    for key in ("hives", "splits", "queens", "inspections"):
        assert isinstance(payload[key], list)
        assert payload[key]
    assert payload["apiary"] == "Willow Yard Apiary"


def test_export_csv_writes_header_and_rows(apiary, tmp_path):
    target = tmp_path / "hives.csv"
    result = ok(apiary, "export", "--format", "csv", "--out", str(target))
    assert result.out == ""
    assert f"to {target}" in result.err
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("id,label,kind,yard,established_date,status")
    assert len(lines) == 1 + len(read_store(apiary)["hives"])


def test_export_csv_of_empty_apiary_is_header_only(store):
    ok(store, "init", "--apiary", "Willow Yard")
    ok(store, "unlock")
    result = ok(store, "export", "--format", "csv")
    lines = result.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("id,label")


def test_export_refuses_while_locked(apiary):
    (apiary.parent / ".hivesplit.session").unlink()
    result = hivesplit(apiary, "export", "--format", "json")
    assert result.code == 1
    assert "Locked — run hivesplit unlock" in result.err
    assert result.out == ""


# --------------------------------------------------------------------------
# T11 — output polish
# --------------------------------------------------------------------------


def test_long_label_does_not_break_alignment(apiary):
    long_label = "North Meadow Long Colony Label Two"
    ok(apiary, "hive", "add", "--label", long_label, "--yard", "South")
    lines = ok(apiary, "hive", "list").out.splitlines()
    header = lines[0]
    values = [line for line in lines[2:] if line.strip()]
    assert any(long_label in line for line in values)
    status_column = header.index("STATUS")
    for line in values:
        assert line[status_column:].strip() == "active"


def test_hive_list_json_is_an_array_only(apiary):
    result = ok(apiary, "hive", "list", "--json")
    payload = json.loads(result.out)
    assert isinstance(payload, list)
    assert payload[0]["label"] == "H1"


def test_hive_show_and_queen_show_json(apiary):
    ok(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-08", "--source", "grafted")
    hive = json.loads(ok(apiary, "hive", "show", "H2", "--json").out)
    assert hive["current_queen"]["id"] == "Q1"
    queen = json.loads(ok(apiary, "queen", "show", "Q1", "--json").out)
    assert queen["installed_hive_id"] == "H2"


def test_risk_json_shape(apiary):
    ok(apiary, "queen", "add", "--hive", "H2", "--emerged", "2025-05-08", "--source", "grafted")
    inspect_h2(apiary)
    payload = json.loads(ok(apiary, "risk", "--hive", "H2", "--json").out)
    assert set(("score", "band", "factors")).issubset(payload)
    assert isinstance(payload["score"], int)
    assert payload["band"] in ("low", "elevated", "high")
    assert all({"label", "points"} <= set(factor) for factor in payload["factors"])


def test_output_contains_no_ansi_colour(apiary):
    populated(apiary)
    for args in (("hive", "list"), ("tree",), ("queen", "list"),
                 ("inspect", "list", "--hive", "H2"), ("risk",), ("status",)):
        assert "\x1b[" not in ok(apiary, *args).out


# --------------------------------------------------------------------------
# T12 — status and cadence
# --------------------------------------------------------------------------


def test_status_separates_overdue_from_never_inspected(apiary):
    ok(apiary, "inspect", "add", "--hive", "H1", "--date", days_ago(20), "--crowding", "2")
    output = ok(apiary, "status").out
    overdue_block = output.split("Not inspected in 14+ days")[1].split("Never inspected")[0]
    never_block = output.split("Never inspected")[1]
    assert "H1" in overdue_block
    assert "20d" in overdue_block
    assert "H2" in never_block
    assert "H2" not in overdue_block


def test_status_days_override_moves_hive_out_of_overdue(apiary):
    ok(apiary, "inspect", "add", "--hive", "H1", "--date", days_ago(20), "--crowding", "2")
    output = ok(apiary, "status", "--days", "30").out
    assert "Not inspected in 30+ days" in output
    overdue_block = output.split("Not inspected in 30+ days")[1].split("Never inspected")[0]
    assert "H1" not in overdue_block


def test_status_check_first_block_is_ranked(apiary):
    ok(apiary, "hive", "add", "--label", "H3")
    inspect_h2(apiary)
    ok(apiary, "inspect", "add", "--hive", "H1", "--date", "2025-06-01",
       "--queen-cells", "5", "--crowding", "3")
    ok(apiary, "inspect", "add", "--hive", "H3", "--date", "2025-06-01", "--stores", "3")
    output = ok(apiary, "status").out
    block = output.split("Check first (highest swarm risk)")[1].split("Not inspected")[0]
    rows = [line for line in block.splitlines() if line.strip() and not line.startswith("HIVE") and not line.startswith("----")]
    assert rows[0].startswith("H1")


def test_status_on_empty_apiary(store):
    ok(store, "init", "--apiary", "Willow Yard")
    result = ok(store, "status")
    assert result.out.strip() == "No hives yet"


def test_status_json_payload(apiary):
    ok(apiary, "inspect", "add", "--hive", "H1", "--date", days_ago(20), "--crowding", "2")
    payload = json.loads(ok(apiary, "status", "--json").out)
    assert payload["hives"] == 2
    assert [row["hive"] for row in payload["overdue"]] == ["H1"]
    assert [row["hive"] for row in payload["never_inspected"]] == ["H2"]
