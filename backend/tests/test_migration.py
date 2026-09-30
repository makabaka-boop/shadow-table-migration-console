import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, quote_ident


VALID_MAPPING = {
    "id": {"op": "copy", "source": "legacy_id"},
    "code": {"op": "trim", "source": "legacy_code"},
    "name": {"op": "trim", "source": "legacy_name"},
    "age": {"op": "parse_decimal", "source": "legacy_age"},
}


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "migration.db"
    app = create_app(str(db_path))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, db_path


def tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def repair_initial_rows(client):
    updates = [
        (3, "D400", "Duplicate Code", "42"),
        (4, "B200", "Too Young", "1"),
        (5, "C300", "Bad Age", "29"),
    ]
    revision = None
    for row_id, code, name, age in updates:
        response = client.post(
            "/api/legacy",
            json={
                "id": row_id,
                "legacy_code": code,
                "legacy_name": name,
                "legacy_age": age,
            },
        )
        assert response.status_code == 200
        revision = response.json()["legacy_revision"]
    return revision


def test_preview_reports_all_failed_rows_and_sources_without_changing_records(client):
    http, db_path = client
    before = http.get("/api/state").json()

    response = http.post("/api/preview", json={"mapping": VALID_MAPPING})

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert result["source_revision"] == 1
    assert result["total_rows"] == 5
    assert [row["legacy_id"] for row in result["failed_rows"]] == [3, 4, 5]

    by_legacy = {row["legacy_id"]: row for row in result["failed_rows"]}
    duplicate = by_legacy[3]["errors"]
    assert any(error["code"] == "unique_violation" and error["field"] == "code" for error in duplicate)
    assert by_legacy[3]["sources"]["code"] == "去首尾空白 legacy_code"
    range_error = by_legacy[4]["errors"][0]
    assert range_error["code"] == "out_of_range"
    assert range_error["field"] == "age"
    parse_error = by_legacy[5]["errors"][0]
    assert parse_error["code"] == "not_integer"
    assert parse_error["source"] == "十进制整数 legacy_age"

    after = http.get("/api/state").json()
    assert after["records"] == before["records"] == []
    assert after["meta"]["target_revision"] == 0
    assert after["history"] == []
    assert any(name.startswith("mig_shadow_") for name in tables(db_path))


def test_stale_preview_is_rejected_after_interleaved_write_then_recompute_commits(client):
    http, db_path = client
    revision = repair_initial_rows(http)
    assert revision == 4

    preview = http.post("/api/preview", json={"mapping": VALID_MAPPING})
    assert preview.status_code == 200
    first = preview.json()
    assert first["ok"] is True
    assert first["source_revision"] == 4
    assert len(first["candidate_rows"]) == 5

    # Another client writes while the first user is reviewing the preview.
    other_client = TestClient(http.app, raise_server_exceptions=False)
    try:
        other_write = other_client.post(
            "/api/legacy",
            json={"legacy_code": "E500", "legacy_name": " Later Writer ", "legacy_age": "33"},
        )
    finally:
        other_client.close()
    assert other_write.status_code == 200
    assert other_write.json()["legacy_revision"] == 5

    stale_commit = http.post(
        f"/api/migrations/{first['job_id']}/commit", json={"source_revision": 4}
    )
    assert stale_commit.status_code == 409
    assert "重新预演" in stale_commit.json()["detail"]

    state_after_reject = http.get("/api/state").json()
    assert state_after_reject["meta"]["target_revision"] == 0
    assert state_after_reject["records"] == []
    assert state_after_reject["history"] == []

    recomputed = http.post("/api/preview", json={"mapping": VALID_MAPPING}).json()
    assert recomputed["ok"] is True
    assert recomputed["source_revision"] == 5
    commit = http.post(
        f"/api/migrations/{recomputed['job_id']}/commit",
        json={"source_revision": 5},
    )
    assert commit.status_code == 200
    switched = commit.json()
    assert switched["target_revision"] == 1
    assert switched["history_table"] == "records_history_v0"

    state = http.get("/api/state").json()
    assert state["meta"] == {
        "id": 1,
        "legacy_revision": 5,
        "target_revision": 1,
        "active_table": "records",
    }
    assert len(state["records"]) == 6
    assert {row["code"] for row in state["records"]} == {
        "A100",
        "A101",
        "D400",
        "B200",
        "C300",
        "E500",
    }
    assert all(row["source_revision"] == 5 for row in state["records"])

    history = http.get("/api/history/0")
    assert history.status_code == 200
    assert history.json()["records"] == []
    assert history.json()["read_only"] is True

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="read only"):
            conn.execute(
                'INSERT INTO records_history_v0(id, code, name, age, source_revision) '
                "VALUES (1, 'X', 'X', 1, 1)"
            )
    finally:
        conn.close()

    # A later source revision and a second switch must retain the previous formal
    # table as another read-only history version, not overwrite v0.
    add = http.post(
        "/api/legacy",
        json={"legacy_code": "F600", "legacy_name": "Second Switch", "legacy_age": "21"},
    )
    assert add.json()["legacy_revision"] == 6
    next_preview = http.post("/api/preview", json={"mapping": VALID_MAPPING}).json()
    assert next_preview["source_revision"] == 6
    next_commit = http.post(
        f"/api/migrations/{next_preview['job_id']}/commit", json={"source_revision": 6}
    )
    assert next_commit.status_code == 200
    assert next_commit.json()["target_revision"] == 2
    assert next_commit.json()["history_table"] == "records_history_v1"

    second_state = http.get("/api/state").json()
    assert len(second_state["records"]) == 7
    assert {item["version"] for item in second_state["history"]} == {0, 1}
    assert http.get("/api/history/1").json()["records"] == state["records"]


def test_copy_interruption_leaves_no_partial_shadow_and_keeps_formal_table(client):
    http, db_path = client
    repair_initial_rows(http)
    before = http.get("/api/state").json()

    response = http.post(
        "/api/preview",
        json={"mapping": VALID_MAPPING},
        headers={"X-Fault-Inject": "preview-copy-abort"},
    )
    assert response.status_code == 500

    after = http.get("/api/state").json()
    assert after["records"] == before["records"]
    assert after["meta"]["target_revision"] == 0
    assert not any(name.startswith("mig_shadow_") or name.startswith("mig_valid_") for name in tables(db_path))


def test_switch_failure_is_atomic_and_can_commit_after_no_intervening_change(client):
    http, db_path = client
    repair_initial_rows(http)
    preview = http.post("/api/preview", json={"mapping": VALID_MAPPING}).json()
    assert preview["ok"] is True

    failed = http.post(
        f"/api/migrations/{preview['job_id']}/commit",
        json={"source_revision": preview["source_revision"]},
        headers={"X-Fault-Inject": "commit-switch-after-rename"},
    )
    assert failed.status_code == 500

    state = http.get("/api/state").json()
    assert state["records"] == []
    assert state["meta"]["target_revision"] == 0
    assert state["history"] == []
    table_names = tables(db_path)
    assert "records" in table_names
    assert "records_history_v0" not in table_names
    assert any(name.startswith("mig_valid_") for name in table_names)

    retry = http.post(
        f"/api/migrations/{preview['job_id']}/commit",
        json={"source_revision": preview["source_revision"]},
    )
    assert retry.status_code == 200
    assert retry.json()["target_revision"] == 1
    assert len(http.get("/api/state").json()["records"]) == 5


def test_switch_failure_before_rename_leaves_no_history_table(client):
    http, db_path = client
    repair_initial_rows(http)
    preview = http.post("/api/preview", json={"mapping": VALID_MAPPING}).json()

    response = http.post(
        f"/api/migrations/{preview['job_id']}/commit",
        json={"source_revision": preview["source_revision"]},
        headers={"X-Fault-Inject": "commit-switch-before-rename"},
    )
    assert response.status_code == 500

    state = http.get("/api/state").json()
    assert state["meta"]["target_revision"] == 0
    assert state["records"] == []
    assert "records_history_v0" not in tables(db_path)
    assert http.post(
        f"/api/migrations/{preview['job_id']}/commit",
        json={"source_revision": preview["source_revision"]},
    ).status_code == 200


def test_constant_and_integer_range_mapping_are_validated(client):
    http, _ = client
    good = http.post(
        "/api/preview",
        json={
            "mapping": {
                "id": {"op": "constant", "value": "42"},
                "code": {"op": "constant", "value": " K "},
                "name": {"op": "trim", "source": "legacy_name"},
                "age": {"op": "constant", "value": 99},
            }
        },
    )
    assert good.status_code == 200
    row = good.json()["candidate_rows"][0]
    assert row["values"] == {
        "id": 42,
        "code": " K ",
        "name": "Ada Lovelace",
        "age": 99,
        "source_revision": 1,
    }
    assert row["sources"]["id"] == "常量 42"

    bad = http.post(
        "/api/preview",
        json={
            "mapping": {
                "id": {"op": "constant", "value": "1.5"},
                "code": {"op": "constant", "value": "K"},
                "name": {"op": "trim", "source": "legacy_name"},
                "age": {"op": "constant", "value": 99},
            }
        },
    )
    assert bad.status_code == 400
    assert "十进制整数" in bad.json()["detail"]
