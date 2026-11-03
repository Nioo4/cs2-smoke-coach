"""Pure audit maintenance/TOC boundaries; no SQL, process, HTTP or settings access."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.scripts import restore
from app.services import tactics_maintenance as maintenance

V10 = "0010_tactics_shares_history"
V11 = "0011_tactics_audit_events"
AUDIT = "tactic_audit_events"


def result(rows):
    class Rows(list):
        def all(self):
            return self

    return SimpleNamespace(scalars=lambda: Rows(rows), all=lambda: rows)


class Catalog:
    """Catalog responses only; unexpected reads fail instead of opening a connection."""

    def __init__(self, version=V10, *, names=None):
        self.names = set(maintenance.FORMAL_TABLES if names is None else names)
        if names is None and version == V11:
            self.names.add(AUDIT)
        self.version, self.schema, self.queries = version, "public", []
        self.columns = [
            ("id", "uuid", "NO", None),
            ("actor_id", "uuid", "YES", None),
            ("tactic_id", "uuid", "YES", None),
            ("team_id", "uuid", "YES", None),
            ("resource_id", "uuid", "YES", None),
            ("action", "character varying", "NO", 64),
            ("result_code", "character varying", "NO", 64),
            ("request_id", "character varying", "NO", 32),
            ("created_at", "timestamp with time zone", "NO", None),
        ]
        self.constraints = [
            ("p", "tactic_audit_events_pkey", " "),
            ("c", "ck_tactic_audit_action", " "),
            ("c", "ck_tactic_audit_result", " "),
            ("c", "ck_tactic_audit_request", " "),
            ("f", "actor_fk", "n"), ("f", "tactic_fk", "n"), ("f", "team_fk", "n"),
        ]
        self.indexes = 3

    async def execute(self, query):
        sql = str(query)
        self.queries.append(sql)
        if "FROM pg_tables" in sql:
            return result(self.names)
        if "information_schema.columns" in sql:
            return result(self.columns)
        if "FROM pg_constraint" in sql:
            return result(self.constraints)
        pytest.fail("Unexpected catalog execute")

    async def scalar(self, query):
        sql = str(query)
        self.queries.append(sql)
        if "version_num FROM alembic_version" in sql:
            return self.version
        if "current_schema()" in sql:
            return self.schema
        if "FROM pg_indexes" in sql:
            return self.indexes
        pytest.fail("Unexpected catalog scalar")


@pytest.mark.parametrize("version", [V10, V11])
async def test_exact_supported_revision_is_returned_without_querying_absent_audit(version):
    catalog = Catalog(version)
    assert await maintenance.formal_schema(catalog) == version
    audit_catalog_reads = [q for q in catalog.queries if AUDIT in q]
    assert bool(audit_catalog_reads) == (version == V11)


async def test_teaching_only_is_legacy_only_when_explicitly_allowed():
    catalog = Catalog(names={"targets", "lineups", "tutorial_videos"})
    assert await maintenance.formal_schema(catalog, legacy_ok=True) is False
    assert len(catalog.queries) == 1  # Does not assume an alembic/audit table exists.
    with pytest.raises(maintenance.MaintenanceError, match="FORMAL_SCHEMA_INCOMPLETE"):
        await maintenance.formal_schema(catalog)


@pytest.mark.parametrize("version,names,code", [
    (V11, {AUDIT}, "FORMAL_SCHEMA_INCOMPLETE"),
    (V10, {"users"}, "FORMAL_SCHEMA_INCOMPLETE"),
    (V11, maintenance.FORMAL_TABLES - {"tactic_documents"} | {AUDIT},
     "FORMAL_SCHEMA_INCOMPLETE"),
    (V10, maintenance.FORMAL_TABLES | {AUDIT}, "SCHEMA_REJECTED"),
    (V11, maintenance.FORMAL_TABLES, "SCHEMA_REJECTED"),
    ("0012_unknown", maintenance.FORMAL_TABLES | {AUDIT}, "SCHEMA_REJECTED"),
])
async def test_partial_or_revision_mismatched_catalog_fails_even_in_legacy_mode(
    version, names, code,
):
    with pytest.raises(maintenance.MaintenanceError, match=code):
        await maintenance.formal_schema(Catalog(version, names=names), legacy_ok=True)


@pytest.mark.parametrize("damage", ["column", "nullable", "check", "fk", "index", "schema"])
async def test_0011_rejects_incomplete_audit_shape(damage):
    catalog = Catalog(V11)
    if damage == "column":
        catalog.columns.pop()
    elif damage == "nullable":
        catalog.columns[1] = ("actor_id", "uuid", "NO", None)
    elif damage == "check":
        catalog.constraints.remove(("c", "ck_tactic_audit_request", " "))
    elif damage == "fk":
        catalog.constraints[-1] = ("f", "team_fk", "a")  # NO ACTION blocks parent cleanup.
    elif damage == "index":
        catalog.indexes = 2
    else:
        catalog.schema = "other"
    with pytest.raises(maintenance.MaintenanceError, match="SCHEMA_REJECTED"):
        await maintenance.formal_schema(catalog)


def toc_lines(tables=(), data=()):
    objects = [f"TABLE public {name} fixture_owner" for name in sorted(tables)]
    objects += [f"TABLE DATA public {name} fixture_owner" for name in sorted(data)]
    return "\n".join(f"{index}; 0 0 {obj}" for index, obj in enumerate(objects, 1)).encode()


@pytest.mark.parametrize("tables,data,expected", [
    ({"lineups", "targets"}, set(), False),
    (maintenance.FORMAL_TABLES, set(), True),
    (maintenance.FORMAL_TABLES | {AUDIT}, {AUDIT}, True),
    ({AUDIT}, set(), None),
    (set(), {AUDIT}, None),
    ({"lineups"}, {AUDIT}, None),
    (maintenance.FORMAL_TABLES, {AUDIT}, None),
    (maintenance.FORMAL_TABLES - {"tactics"} | {AUDIT}, set(), None),
])
def test_restore_classifies_complete_dumps_and_rejects_audit_half_sets(
    monkeypatch, tmp_path, tables, data, expected,
):
    monkeypatch.setattr(restore, "validate_backup_group", lambda _group: {
        "database": {"path": "database.dump"},
    })

    def list_only(command, **_kwargs):
        assert command == ["pg_restore", "--list", str(tmp_path / "database.dump")]
        return SimpleNamespace(stdout=toc_lines(tables, data))

    monkeypatch.setattr(restore.subprocess, "run", list_only)
    if expected is None:
        with pytest.raises(restore.RestoreError, match="RESTORE_FORMAL_SCHEMA_INCOMPLETE"):
            restore.backup_contains_formal_tables(tmp_path)
    else:
        assert restore.backup_contains_formal_tables(tmp_path) is expected


@pytest.mark.parametrize("version", [V10, V11])
@pytest.mark.parametrize("execute", [False, True])
async def test_cleanup_keeps_schema_scope_and_audit_retention_in_before_and_after(
    monkeypatch, version, execute,
):
    """Exercise public orchestration, catching lost with_audit in final counts too."""
    at = datetime(2026, 10, 30, tzinfo=UTC)
    queries = []

    class Connection:
        @asynccontextmanager
        async def begin(self):
            yield self

        async def execute(self, query, params=None):
            queries.append((str(query), params))
            return result([])  # Empty candidates; no destructive statements are needed.

        async def scalar(self, query, params=None):
            sql = str(query)
            queries.append((sql, params))
            if "CURRENT_TIMESTAMP" in sql:
                return at
            if "pg_try_advisory_lock" in sql:
                return True
            if "count(*)" in sql:
                return 7 if f"FROM {AUDIT}" in sql else 0
            pytest.fail("Unexpected cleanup scalar")

        async def commit(self):
            pass

    class Engine:
        disposed = False

        @asynccontextmanager
        async def connect(self):
            yield Connection()

        async def dispose(self):
            self.disposed = True

    async def checked_schema(_connection):
        return version

    async def checked_identity(*_args):
        pass

    engine = Engine()
    monkeypatch.setattr(maintenance, "scope", lambda *_args, **_kwargs: (
        SimpleNamespace(database="pure_stub"), str(uuid.uuid4()),
    ))
    monkeypatch.setattr(maintenance, "create_async_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(maintenance, "formal_schema", checked_schema)
    monkeypatch.setattr(maintenance, "identity", checked_identity)
    response = await maintenance.cleanup("unused", "unused", uuid.uuid4(), execute=execute)
    assert response["executed"] is execute and engine.disposed
    counts = response["remaining"] if execute else response["candidates"]
    audit_queries = [(sql, params) for sql, params in queries if AUDIT in sql]
    if version == V10:
        assert AUDIT not in counts and not audit_queries
        assert not execute or AUDIT not in response["removed"]
    else:
        assert counts[AUDIT] == 7
        count_queries = [(sql, params) for sql, params in audit_queries if "count(*)" in sql]
        assert len(count_queries) == (2 if execute else 1)
        for sql, params in audit_queries:
            assert "created_at<=:at - INTERVAL '30 days'" in sql and params["at"] == at
        if execute:
            assert response["removed"][AUDIT] == 0
            assert any("ORDER BY id LIMIT :limit" in sql for sql, _ in audit_queries)
