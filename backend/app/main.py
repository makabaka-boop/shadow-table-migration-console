from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.getenv("MIGRATION_DB", os.path.join(os.path.dirname(__file__), "..", "migration.db"))
CURRENT_DB_PATH = DB_PATH
INT32_MIN = 1
INT32_MAX = 2**31 - 1

TARGET_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "required": True, "min": 1, "max": INT32_MAX, "unique": True},
    "code": {"type": "text", "required": True, "unique": True},
    "name": {"type": "text", "required": True},
    "age": {"type": "integer", "required": True, "min": 0, "max": 150},
}
OPERATIONS = {"copy", "trim", "parse_decimal", "constant"}


class MappingField(BaseModel):
    op: Literal["copy", "trim", "parse_decimal", "constant"]
    source: str | None = None
    value: str | int | None = None


class PreviewRequest(BaseModel):
    mapping: dict[str, MappingField]


class CommitRequest(BaseModel):
    source_revision: int


class LegacyWrite(BaseModel):
    id: int | None = None
    legacy_code: str
    legacy_name: str
    legacy_age: str


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"unsafe SQL identifier: {name}")
    return f'"{name}"'


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or CURRENT_DB_PATH
    os.makedirs(os.path.abspath(os.path.dirname(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def records_sql(table: str) -> str:
    q = quote_ident(table)
    return f"""
        CREATE TABLE {q} (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            age INTEGER NOT NULL CHECK(age BETWEEN 0 AND 150),
            source_revision INTEGER NOT NULL
        )
    """


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        with transaction(conn):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_meta(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    legacy_revision INTEGER NOT NULL DEFAULT 1,
                    target_revision INTEGER NOT NULL DEFAULT 0,
                    active_table TEXT NOT NULL DEFAULT 'records'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_jobs(
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('ready','committed')),
                    source_revision INTEGER NOT NULL,
                    target_revision INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    shadow_table TEXT NOT NULL,
                    valid_table TEXT,
                    mapping_json TEXT NOT NULL,
                    failures_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    committed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_history(
                    version INTEGER PRIMARY KEY,
                    source_revision INTEGER NOT NULL,
                    table_name TEXT NOT NULL UNIQUE,
                    committed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(records_sql("records"))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS legacy_records(
                    id INTEGER PRIMARY KEY,
                    legacy_code TEXT,
                    legacy_name TEXT,
                    legacy_age TEXT
                )
                """
            )
            exists = conn.execute("SELECT 1 FROM app_meta WHERE id=1").fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO app_meta(id, legacy_revision, target_revision, active_table) VALUES(1,1,0,'records')"
                )
            count = conn.execute("SELECT COUNT(*) FROM legacy_records").fetchone()[0]
            if count == 0:
                conn.executemany(
                    "INSERT INTO legacy_records(id, legacy_code, legacy_name, legacy_age) VALUES(?,?,?,?)",
                    [
                        (1, "A100", " Ada Lovelace ", "36"),
                        (2, "A101", "Grace Hopper", "85"),
                        (3, "A100", "Duplicate Code", "42"),
                        (4, "B200", "Too Young", "-1"),
                        (5, "C300", "Bad Age", "not-an-int"),
                    ],
                )
            # The trigger records every externally visible source-table write.  It is
            # installed after seed data is loaded, so the initial snapshot is revision 1.
            trigger = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='legacy_revision_bump'"
            ).fetchone()
            if not trigger:
                conn.execute(
                    """
                    CREATE TRIGGER legacy_revision_bump
                    AFTER INSERT ON legacy_records
                    BEGIN
                        UPDATE app_meta SET legacy_revision=legacy_revision+1 WHERE id=1;
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER legacy_revision_bump_u
                    AFTER UPDATE ON legacy_records
                    BEGIN
                        UPDATE app_meta SET legacy_revision=legacy_revision+1 WHERE id=1;
                    END
                    """
                )
    finally:
        conn.close()


def meta(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM app_meta WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("database is not initialized")
    return row


def source_columns(conn: sqlite3.Connection) -> set[str]:
    cols = {r[1] for r in conn.execute('PRAGMA table_info("legacy_records")')}
    cols.add("legacy_id")
    return cols


def validate_mapping_config(conn: sqlite3.Connection, mapping: dict[str, MappingField]) -> dict[str, dict[str, Any]]:
    allowed_cols = source_columns(conn)
    normalized: dict[str, dict[str, Any]] = {}
    unknown = set(mapping) - set(TARGET_FIELDS)
    missing = set(TARGET_FIELDS) - set(mapping)
    if unknown:
        raise HTTPException(400, f"未知目标字段: {', '.join(sorted(unknown))}")
    if missing:
        raise HTTPException(400, f"目标字段缺少映射: {', '.join(sorted(missing))}")
    for field, spec in mapping.items():
        if spec.op != "constant" and not spec.source:
            raise HTTPException(400, f"{field} 的 {spec.op} 映射缺少 source")
        if spec.op == "constant" and spec.value is None:
            raise HTTPException(400, f"{field} 的常量映射缺少 value")
        if spec.source and spec.source not in allowed_cols:
            raise HTTPException(400, f"{field} 引用了不存在的源列 {spec.source}")
        value = spec.value
        if spec.op == "constant" and TARGET_FIELDS[field]["type"] == "integer":
            if isinstance(value, str):
                parsed = parse_decimal_integer(value)
                if parsed is None:
                    raise HTTPException(400, f"{field} 的常量不是十进制整数")
                value = parsed
            elif not isinstance(value, int):
                raise HTTPException(400, f"{field} 的常量不是整数")
            check_integer_range(field, value)
        normalized[field] = {"op": spec.op, "source": spec.source, "value": value}
    return normalized


def parse_decimal_integer(raw: Any) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if not isinstance(raw, str) or not re.fullmatch(r"[+-]?[0-9]+", raw):
        return None
    return int(raw, 10)


def check_integer_range(field: str, value: int) -> str | None:
    rule = TARGET_FIELDS[field]
    if value < rule["min"] or value > rule["max"]:
        return f"{field} 超出整数范围 {rule['min']}..{rule['max']}"
    return None


def source_value(row: sqlite3.Row, column: str) -> Any:
    if column == "legacy_id":
        return row["id"]
    return row[column]


def describe_source(field: str, spec: dict[str, Any]) -> str:
    op = spec["op"]
    if op == "copy":
        return f"复制 {spec['source']}"
    if op == "trim":
        return f"去首尾空白 {spec['source']}"
    if op == "parse_decimal":
        return f"十进制整数 {spec['source']}"
    return f"常量 {spec['value']!r}"


def apply_mapping(row: sqlite3.Row, field: str, spec: dict[str, Any], source_revision: int) -> tuple[Any, str]:
    if field == "source_revision":
        return source_revision, "源表修订号"
    op = spec["op"]
    source_name = describe_source(field, spec)
    if op == "constant":
        return spec["value"], source_name
    raw = source_value(row, spec["source"])
    if op == "copy":
        return raw, source_name
    if op == "trim":
        return raw.strip() if isinstance(raw, str) else raw, source_name
    return raw, source_name


def preview_migration(
    conn: sqlite3.Connection,
    mapping_req: dict[str, MappingField],
    fault: str | None,
) -> dict[str, Any]:
    mapping = validate_mapping_config(conn, mapping_req)
    revision = meta(conn)["legacy_revision"]
    target_revision = meta(conn)["target_revision"]
    rows = conn.execute(
        'SELECT id, legacy_code, legacy_name, legacy_age FROM "legacy_records" ORDER BY id'
    ).fetchall()

    job_id = uuid.uuid4().hex
    shadow = f"mig_shadow_{job_id}"
    valid = f"mig_valid_{job_id}"
    shadow_q = quote_ident(shadow)
    conn.execute(
        f"""
        CREATE TABLE {shadow_q}(
            row_num INTEGER PRIMARY KEY,
            legacy_id INTEGER NOT NULL,
            id TEXT,
            code TEXT,
            name TEXT,
            age TEXT,
            source_revision INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('valid','invalid')),
            sources_json TEXT NOT NULL,
            errors_json TEXT NOT NULL
        )
        """
    )

    # Switching replaces the complete target table.  Unique constraints are therefore
    # evaluated inside the candidate table only, not against the soon-to-be-history
    # formal table; otherwise re-migrating the same primary keys would always fail.
    seen_unique: dict[tuple[str, Any], int] = {}
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for offset, row in enumerate(rows, start=1):
        sources: dict[str, str] = {}
        errors: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        for field in TARGET_FIELDS:
            if field not in mapping:
                values[field] = None
                sources[field] = "未映射"
                continue
            raw_value, source = apply_mapping(row, field, mapping[field], revision)
            values[field] = raw_value
            sources[field] = source
            rule = TARGET_FIELDS[field]
            if rule["type"] == "integer":
                parsed = parse_decimal_integer(raw_value)
                if parsed is None:
                    errors.append(
                        {"field": field, "code": "not_integer", "message": f"{field} 不是十进制整数", "source": source}
                    )
                    values[field] = raw_value
                else:
                    values[field] = parsed
                    range_error = check_integer_range(field, parsed)
                    if range_error:
                        errors.append({"field": field, "code": "out_of_range", "message": range_error, "source": source})
            elif rule.get("required") and (raw_value is None or (isinstance(raw_value, str) and not raw_value.strip())):
                errors.append({"field": field, "code": "required", "message": f"{field} 不允许为空", "source": source})

        values["source_revision"] = revision
        sources["source_revision"] = "源表修订号"

        # Primary-key and UNIQUE checks are included even if the row has another
        # error, so every conflicting row is reported rather than only the first.
        for field in ("id", "code"):
            value = values.get(field)
            key = (field, value)
            if value is None:
                continue
            if field == "id" and any(e["field"] == "id" for e in errors):
                continue
            if key in seen_unique:
                first = seen_unique[key]
                where = f"第 {first} 行"
                errors.append(
                    {
                        "field": field,
                        "code": "unique_violation",
                        "message": f"{field}={value!r} 与{where}重复",
                        "source": sources[field],
                    }
                )
            else:
                seen_unique[key] = offset

        status = "invalid" if errors else "valid"
        candidate = {
            "row_num": offset,
            "legacy_id": row["id"],
            "values": values,
            "sources": sources,
            "errors": errors,
            "status": status,
        }
        candidates.append(candidate)
        if errors:
            failures.append(candidate)
        conn.execute(
            f"""
            INSERT INTO {shadow_q}
              (row_num, legacy_id, id, code, name, age, source_revision, status, sources_json, errors_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                offset,
                row["id"],
                str(values["id"]) if values["id"] is not None else None,
                values["code"],
                values["name"],
                str(values["age"]) if values["age"] is not None else None,
                revision,
                status,
                json.dumps(sources, ensure_ascii=False),
                json.dumps(errors, ensure_ascii=False),
            ),
        )
        if fault == "preview-copy-abort":
            raise RuntimeError("fault: copy interrupted after a partial row copy")

    if not failures:
        conn.execute(records_sql(valid))
        valid_q = quote_ident(valid)
        for item in candidates:
            conn.execute(
                f"INSERT INTO {valid_q}(id, code, name, age, source_revision) VALUES(?,?,?,?,?)",
                (
                    item["values"]["id"],
                    item["values"]["code"],
                    item["values"]["name"],
                    item["values"]["age"],
                    revision,
                ),
            )

    conn.execute(
        """
        INSERT INTO migration_jobs(
          job_id, status, source_revision, target_revision, row_count, shadow_table,
          valid_table, mapping_json, failures_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job_id,
            "ready",
            revision,
            target_revision,
            len(candidates),
            shadow,
            valid if not failures else None,
            json.dumps(mapping, ensure_ascii=False),
            json.dumps(failures, ensure_ascii=False, default=str),
            now(),
        ),
    )
    return {
        "job_id": job_id,
        "ok": not failures,
        "source_revision": revision,
        "target_revision": target_revision,
        "total_rows": len(candidates),
        "failed_rows": failures,
        "candidate_rows": [
            {
                "row_num": c["row_num"],
                "legacy_id": c["legacy_id"],
                "values": c["values"],
                "sources": c["sources"],
                "status": c["status"],
                "errors": c["errors"],
            }
            for c in candidates
        ],
    }


def make_history_read_only(conn: sqlite3.Connection, table: str) -> None:
    q = quote_ident(table)
    for action in ("INSERT", "UPDATE", "DELETE"):
        trigger = quote_ident(f"{table}__ro_{action.lower()}")
        conn.execute(
            f"CREATE TRIGGER {trigger} BEFORE {action} ON {q} "
            "BEGIN SELECT RAISE(ABORT, 'history table is read only'); END"
        )


def commit_migration(conn: sqlite3.Connection, job_id: str, source_revision: int, fault: str | None) -> dict[str, Any]:
    job = conn.execute("SELECT * FROM migration_jobs WHERE job_id=?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "预演任务不存在，请重新计算")
    m = meta(conn)
    if job["status"] != "ready":
        raise HTTPException(409, "该预演已经提交")
    if json.loads(job["failures_json"]):
        raise HTTPException(409, "预演仍有失败行，请修正映射或源数据后重新预演")
    if source_revision != job["source_revision"]:
        raise HTTPException(409, "提交携带的源表修订号与预演不一致，请重新预演")
    if m["legacy_revision"] != source_revision:
        raise HTTPException(409, f"源表已被其他写入更新到修订 {m['legacy_revision']}，请重新预演")
    if m["target_revision"] != job["target_revision"]:
        raise HTTPException(409, "正式表已被另一个客户端切换，请重新预演")
    valid_table = job["valid_table"]
    if not valid_table:
        raise HTTPException(409, "预演没有可提交的影子表")

    old_version = job["target_revision"]
    history_table = f"records_history_v{old_version}"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (history_table,)).fetchone():
        raise HTTPException(409, f"历史表 {history_table} 已存在")
    if fault == "commit-switch-before-rename":
        raise RuntimeError("fault: switch failed before rename")

    # Everything from the rename through metadata update is one SQLite transaction.
    # Any trigger or fault here rolls back both renames and the revision update.
    conn.execute('ALTER TABLE records RENAME TO ' + quote_ident(history_table))
    if fault == "commit-switch-after-rename":
        raise RuntimeError("fault: switch failed after rename")
    conn.execute('ALTER TABLE ' + quote_ident(valid_table) + ' RENAME TO records')
    make_history_read_only(conn, history_table)
    committed_at = now()
    conn.execute(
        "INSERT INTO migration_history(version, source_revision, table_name, committed_at) VALUES(?,?,?,?)",
        (old_version, source_revision, history_table, committed_at),
    )
    conn.execute(
        "UPDATE app_meta SET target_revision=?, active_table='records' WHERE id=1",
        (old_version + 1,),
    )
    conn.execute("DROP TABLE " + quote_ident(job["shadow_table"]))
    conn.execute(
        "UPDATE migration_jobs SET status='committed', committed_at=? WHERE job_id=?",
        (committed_at, job_id),
    )
    return {
        "ok": True,
        "job_id": job_id,
        "source_revision": source_revision,
        "target_revision": old_version + 1,
        "history_table": history_table,
    }


def state_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    m = dict(meta(conn))
    legacy = [dict(r) for r in conn.execute("SELECT * FROM legacy_records ORDER BY id")]
    records = [dict(r) for r in conn.execute("SELECT * FROM records ORDER BY id")]
    history = [dict(r) for r in conn.execute("SELECT * FROM migration_history ORDER BY version")]
    return {"meta": m, "legacy_records": legacy, "records": records, "history": history}


def create_app(db_path: str = DB_PATH) -> FastAPI:
    global CURRENT_DB_PATH
    CURRENT_DB_PATH = db_path
    init_db(db_path)
    app = FastAPI(title="SQLite shadow-table migration", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        conn = connect()
        try:
            return state_payload(conn)
        finally:
            conn.close()

    @app.post("/api/preview")
    def post_preview(
        payload: PreviewRequest,
        x_fault_inject: str | None = Header(default=None),
    ) -> dict[str, Any]:
        conn = connect()
        try:
            with transaction(conn):
                return preview_migration(conn, payload.mapping, x_fault_inject)
        finally:
            conn.close()

    @app.post("/api/migrations/{job_id}/commit")
    def post_commit(
        job_id: str,
        payload: CommitRequest,
        x_fault_inject: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(404, "预演任务不存在")
        conn = connect()
        try:
            with transaction(conn):
                return commit_migration(conn, job_id, payload.source_revision, x_fault_inject)
        finally:
            conn.close()

    @app.post("/api/legacy")
    def upsert_legacy(row: LegacyWrite) -> dict[str, Any]:
        conn = connect()
        try:
            with transaction(conn):
                if row.id is None:
                    cur = conn.execute(
                        "INSERT INTO legacy_records(legacy_code, legacy_name, legacy_age) VALUES(?,?,?)",
                        (row.legacy_code, row.legacy_name, row.legacy_age),
                    )
                    row_id = cur.lastrowid
                else:
                    exists = conn.execute("SELECT 1 FROM legacy_records WHERE id=?", (row.id,)).fetchone()
                    if not exists:
                        raise HTTPException(404, f"旧表行 {row.id} 不存在")
                    conn.execute(
                        "UPDATE legacy_records SET legacy_code=?, legacy_name=?, legacy_age=? WHERE id=?",
                        (row.legacy_code, row.legacy_name, row.legacy_age, row.id),
                    )
                    row_id = row.id
                saved = conn.execute("SELECT * FROM legacy_records WHERE id=?", (row_id,)).fetchone()
                m = meta(conn)
                return {"row": dict(saved), "legacy_revision": m["legacy_revision"]}
        finally:
            conn.close()

    @app.get("/api/history/{version}")
    def get_history(version: int) -> dict[str, Any]:
        conn = connect()
        try:
            history = conn.execute(
                "SELECT * FROM migration_history WHERE version=?", (version,)
            ).fetchone()
            if history is None:
                raise HTTPException(404, "历史版本不存在")
            table = history["table_name"]
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {quote_ident(table)} ORDER BY id")]
            return {"history": dict(history), "records": rows, "read_only": True}
        finally:
            conn.close()

    return app


# Importing the module for unit tests should not bind an application instance;
# use `uvicorn app.main:app` or create_app() explicitly.
app = create_app() if os.getenv("MIGRATION_AUTO_APP") == "1" else None
