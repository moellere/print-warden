import sqlite3

import pytest

from print_warden.jobs import JobStore


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


def test_happy_path_and_log(store):
    j = store.create(printer="p", name="n", material="PLA")
    for state in ("staged", "awaiting_approval", "approved", "printing", "done"):
        j = store.transition(j["id"], state, f"to {state}")
    assert j["state"] == "done"
    assert [e["to"] for e in j["log"]] == [
        "staged",
        "awaiting_approval",
        "approved",
        "printing",
        "done",
    ]


def test_illegal_transitions_raise(store):
    j = store.create(printer="p", name="n")
    with pytest.raises(ValueError, match="cannot move"):
        store.transition(j["id"], "printing")
    j = store.transition(j["id"], "skipped")
    with pytest.raises(ValueError):
        store.transition(j["id"], "staged")
    with pytest.raises(ValueError):
        store.transition(j["id"], "bogus")


def test_note_keeps_state_and_sets_fields(store):
    j = store.create(printer="p", name="n")
    j = store.note(j["id"], "hello", spool_id=3)
    assert j["state"] == "sliced" and j["spool_id"] == 3
    assert j["log"][-1]["note"] == "hello"
    with pytest.raises(ValueError, match="unknown job fields"):
        store.note(j["id"], "x", nope=1)


def test_old_database_gains_new_columns(tmp_path):
    path = tmp_path / "jobs.db"
    db = sqlite3.connect(path)
    db.execute(
        "create table jobs (id text primary key, created real, updated real, printer text, name text,"
        " material text, quality text, state text, stl text, gcode text, preview text, remote_name text,"
        " est_seconds integer, filament_g real, layers integer, max_z real, warnings text, authorized_by text,"
        " started_at real, finished_at real, error text, log text)"
    )
    db.execute(
        "insert into jobs (id, state, log, warnings) values ('old', 'staged', '[]', '[]')"
    )
    db.commit()
    db.close()
    store = JobStore(path)
    j = store.note("old", "migrated", spool_id=7)
    assert j["spool_id"] == 7 and j["approval_requested_at"] is None


def test_queue_order_and_json_fields(store):
    a = store.create(printer="p", name="a", offer=True, slice_blockers=["x"])
    b = store.create(printer="p", name="b")
    for j in (a, b):
        store.transition(j["id"], "staged")
    assert [j["name"] for j in store.queue("p")] == ["a", "b"]
    store.note(b["id"], "move", queue_pos=-1.0)
    assert [j["name"] for j in store.queue("p")] == ["b", "a"]
    got = store.get(a["id"])
    assert (
        got["slice_blockers"] == ["x"]
        and got["offer"] is True
        and store.get(b["id"])["offer"] is False
    )
    store.note(a["id"], "waive", slice_blockers=[])
    assert store.get(a["id"])["slice_blockers"] == []
