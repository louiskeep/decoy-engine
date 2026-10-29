"""The ResourceLedger: a local SQLite transactional ownership adapter.

Pattern: SQLite serialized ownership transactions (SQLite lang_transaction +
tempfiles docs, public domain). The ledger is the engine's durable local record
of arena/resource identity, quota reservations, retention class and lifecycle
state. It is a subordinate cache of the platform ownership authority, never a
second lease source of truth (ledger subordination contract 2.1); it stamps rows
with the lease identity the authority issued and never mints or advances a fence.

Transaction discipline (plan 1.5):

* ``BEGIN IMMEDIATE`` for every write, so a writer takes the reserved lock up
  front and two processes cannot both believe they hold the row.
* Conditional updates verify the affected-row count. A CAS that touched zero
  rows means a moved fence or a lost race and raises ``ScratchOwnershipError``;
  it never silently proceeds.
* Durable rollback journaling with full synchronisation (``journal_mode=DELETE``,
  ``synchronous=FULL``). Journaling is not disabled to avoid accounting for it.
* ``temp_store=MEMORY`` for the bounded metadata workload; this does not remove
  the durable rollback journal. ``VACUUM`` and other uncontrolled auxiliary
  databases are never issued.
* One durable control database per managed pool, in a provider-provisioned
  control arena, not one independent authority per process.

The full retention/state schema is written up front so later sweep/key slices do
not reinterpret an old row unsafely.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decoy_engine.scratch._errors import ScratchControlUnavailable, ScratchOwnershipError
from decoy_engine.scratch._policy import (
    AuthorityMode,
    LeaseIdentity,
    ResourceKind,
    ResourceState,
    Retention,
)

FORMAT_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arenas (
    arena_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    authority_mode TEXT NOT NULL,
    root_path TEXT NOT NULL,
    root_dev INTEGER NOT NULL,
    root_ino INTEGER NOT NULL,
    quota_domain_id TEXT NOT NULL,
    quota_bytes INTEGER NOT NULL,
    quota_inodes INTEGER NOT NULL,
    is_control INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    arena_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    authority_mode TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    retention TEXT NOT NULL,
    owner_principal TEXT NOT NULL,
    root_dev INTEGER NOT NULL,
    root_ino INTEGER NOT NULL,
    quota_domain_id TEXT NOT NULL,
    enc_policy_ref TEXT,
    key_version_ref TEXT,
    recovery_class TEXT NOT NULL,
    logical_bytes INTEGER NOT NULL DEFAULT 0,
    disposition TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (arena_id) REFERENCES arenas (arena_id)
);
CREATE INDEX IF NOT EXISTS ix_resources_arena ON resources (arena_id);
"""


@dataclass(frozen=True, slots=True)
class ArenaRow:
    arena_id: str
    job_id: str
    attempt_id: str
    lease_id: str
    generation: int
    authority_mode: AuthorityMode
    root_path: str
    root_dev: int
    root_ino: int
    quota_domain_id: str
    quota_bytes: int
    quota_inodes: int
    is_control: bool
    state: ResourceState


@dataclass(frozen=True, slots=True)
class ResourceRow:
    resource_id: str
    arena_id: str
    job_id: str
    lease_id: str
    generation: int
    authority_mode: AuthorityMode
    kind: ResourceKind
    state: ResourceState
    retention: Retention
    owner_principal: str
    quota_domain_id: str
    logical_bytes: int
    disposition: str | None


def _verify_single_device(paths: tuple[Path, ...]) -> None:
    """Fail unless every control sidecar path resolves to one device.

    The control-arena bootstrap verifies its DB, journal/WAL and liveness-lock
    paths all resolve to one device and filesystem, failing otherwise (plan 1.5,
    A13). A split-device control store could place the journal where the lock's
    fencing does not reach.
    """

    devices: set[int] = set()
    for path in paths:
        probe = path if path.exists() else path.parent
        devices.add(os.stat(probe).st_dev)
    if len(devices) > 1:
        raise ScratchControlUnavailable(
            code="scratch_control_split_device",
            message="control sidecars span more than one device",
        )


class ResourceLedger:
    """One durable control database for a managed pool."""

    def __init__(self, conn: sqlite3.Connection, pool_id: str) -> None:
        self._conn = conn
        self._pool_id = pool_id

    @classmethod
    def bootstrap(
        cls,
        control_dir: Path,
        pool_id: str,
        *,
        busy_timeout_ms: int = 5000,
    ) -> ResourceLedger:
        """Open (creating if needed) the one control DB for ``pool_id``.

        Verifies single-device placement of the DB, its rollback journal and the
        liveness-lock path before opening SQLite (plan 1.5 control-arena
        bootstrap). The caller provisions ``control_dir`` as a 0700, quota-reserved
        control arena first; this does not create an unregistered scratch
        exemption, the control arena's own files are registered by the manager.
        """

        db_path = control_dir / "ledger.db"
        journal_path = control_dir / "ledger.db-journal"
        lock_path = control_dir / "ledger.lock"
        _verify_single_device((db_path, journal_path, lock_path, control_dir))
        try:
            # Create the DB with 0600 metadata permissions from the first open
            # (plan 1.9 A01: metadata is 0600 from creation), under a tight umask
            # so the journal/WAL sidecars SQLite creates inherit it too.
            prev_umask = os.umask(0o077)
            try:
                conn = sqlite3.connect(
                    str(db_path), isolation_level=None, timeout=busy_timeout_ms / 1000
                )
            finally:
                os.umask(prev_umask)
            if db_path.exists():
                os.chmod(db_path, 0o600)
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_SCHEMA)
            cur = conn.execute("SELECT value FROM ledger_meta WHERE key = 'format_version'")
            row = cur.fetchone()
            if row is None:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO ledger_meta (key, value) VALUES ('format_version', ?)",
                    (str(FORMAT_VERSION),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES ('pool_id', ?)",
                    (pool_id,),
                )
                conn.commit()
            elif int(row[0]) != FORMAT_VERSION:
                conn.close()
                raise ScratchControlUnavailable(
                    code="scratch_control_version_mismatch",
                    message=f"ledger format {row[0]} != supported {FORMAT_VERSION}",
                )
        except sqlite3.OperationalError as exc:
            raise ScratchControlUnavailable(message="could not open control database") from exc
        return cls(conn, pool_id)

    # -- writes ------------------------------------------------------------

    def register_arena(
        self,
        *,
        arena_id: str,
        lease: LeaseIdentity,
        root_path: str,
        root_dev: int,
        root_ino: int,
        quota_domain_id: str,
        quota_bytes: int,
        quota_inodes: int,
        is_control: bool,
    ) -> None:
        import time

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO arenas (arena_id, job_id, attempt_id, lease_id, generation, "
                "authority_mode, root_path, root_dev, root_ino, quota_domain_id, quota_bytes, "
                "quota_inodes, is_control, state, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    arena_id,
                    lease.job_id,
                    lease.attempt_id,
                    lease.lease_id,
                    lease.generation,
                    lease.mode.value,
                    root_path,
                    root_dev,
                    root_ino,
                    quota_domain_id,
                    quota_bytes,
                    quota_inodes,
                    int(is_control),
                    ResourceState.ACTIVE.value,
                    time.time(),
                ),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            self._safe_rollback()
            raise ScratchControlUnavailable(message="arena registration failed") from exc

    def register_resource(
        self,
        *,
        resource_id: str,
        arena_id: str,
        lease: LeaseIdentity,
        kind: ResourceKind,
        retention: Retention,
        owner_principal: str,
        root_dev: int,
        root_ino: int,
        quota_domain_id: str,
        recovery_class: str,
        enc_policy_ref: str | None = None,
        key_version_ref: str | None = None,
    ) -> None:
        """Insert a durable ``CREATING`` row and commit it. The manager calls
        this *before* a writable path/FD is returned, so registration always
        precedes the first write (plan 1.5, A01)."""
        import time

        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO resources (resource_id, arena_id, job_id, attempt_id, lease_id, "
                "generation, authority_mode, kind, state, retention, owner_principal, root_dev, "
                "root_ino, quota_domain_id, enc_policy_ref, key_version_ref, recovery_class, "
                "logical_bytes, disposition, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    resource_id,
                    arena_id,
                    lease.job_id,
                    lease.attempt_id,
                    lease.lease_id,
                    lease.generation,
                    lease.mode.value,
                    kind.value,
                    ResourceState.CREATING.value,
                    retention.value,
                    owner_principal,
                    root_dev,
                    root_ino,
                    quota_domain_id,
                    enc_policy_ref,
                    key_version_ref,
                    recovery_class,
                    0,
                    None,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            self._safe_rollback()
            raise ScratchControlUnavailable(message="resource registration failed") from exc

    def transition(
        self,
        resource_id: str,
        *,
        frm: ResourceState,
        to: ResourceState,
        generation: int,
    ) -> None:
        """CAS a resource's lifecycle state under the current fence.

        The ``WHERE`` predicate pins the expected prior state and generation; a
        zero affected-row count is a lost race or a moved fence and raises
        ``ScratchOwnershipError`` (plan 1.5, mutation target: generation
        predicate)."""
        import time

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE resources SET state = ?, updated_at = ? "
                "WHERE resource_id = ? AND state = ? AND generation = ?",
                (to.value, time.time(), resource_id, frm.value, generation),
            )
            affected = cur.rowcount
            if affected != 1:
                self._conn.rollback()
                raise ScratchOwnershipError(
                    message=f"transition {frm.value}->{to.value} touched {affected} rows"
                )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            self._safe_rollback()
            raise ScratchControlUnavailable(message="state transition failed") from exc

    def set_disposition(
        self,
        resource_id: str,
        *,
        disposition: str,
        state: ResourceState,
        retained_bytes: int = 0,
    ) -> None:
        """Record a terminal disposition. This write path is intentionally tiny
        and lives in the control arena's own quota domain, so it can record a
        terminal failure (for example ``scratch_quota_exceeded``) for a data
        arena that is at its own cap (plan 1.5 control-arena sizing, A02)."""
        import time

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE resources SET disposition = ?, state = ?, logical_bytes = ?, "
                "updated_at = ? WHERE resource_id = ?",
                (disposition, state.value, retained_bytes, time.time(), resource_id),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                raise ScratchOwnershipError(message="set_disposition matched no resource")
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            self._safe_rollback()
            raise ScratchControlUnavailable(message="disposition write failed") from exc

    # -- reads -------------------------------------------------------------

    def list_arenas(self) -> list[ArenaRow]:
        cur = self._conn.execute("SELECT * FROM arenas")
        return [self._arena_row(r, cur.description) for r in cur.fetchall()]

    def list_resources(self, arena_id: str | None = None) -> list[ResourceRow]:
        if arena_id is None:
            cur = self._conn.execute("SELECT * FROM resources")
        else:
            cur = self._conn.execute("SELECT * FROM resources WHERE arena_id = ?", (arena_id,))
        return [self._resource_row(r, cur.description) for r in cur.fetchall()]

    def get_resource(self, resource_id: str) -> ResourceRow | None:
        cur = self._conn.execute("SELECT * FROM resources WHERE resource_id = ?", (resource_id,))
        row = cur.fetchone()
        return self._resource_row(row, cur.description) if row else None

    def get_arena(self, arena_id: str) -> ArenaRow | None:
        cur = self._conn.execute("SELECT * FROM arenas WHERE arena_id = ?", (arena_id,))
        row = cur.fetchone()
        return self._arena_row(row, cur.description) if row else None

    def close(self) -> None:
        self._conn.close()

    # -- helpers -----------------------------------------------------------

    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    @staticmethod
    def _arena_row(row: tuple, description: Any) -> ArenaRow:
        d = {col[0]: val for col, val in zip(description, row, strict=True)}
        return ArenaRow(
            arena_id=d["arena_id"],
            job_id=d["job_id"],
            attempt_id=d["attempt_id"],
            lease_id=d["lease_id"],
            generation=d["generation"],
            authority_mode=AuthorityMode(d["authority_mode"]),
            root_path=d["root_path"],
            root_dev=d["root_dev"],
            root_ino=d["root_ino"],
            quota_domain_id=d["quota_domain_id"],
            quota_bytes=d["quota_bytes"],
            quota_inodes=d["quota_inodes"],
            is_control=bool(d["is_control"]),
            state=ResourceState(d["state"]),
        )

    @staticmethod
    def _resource_row(row: tuple, description: Any) -> ResourceRow:
        d = {col[0]: val for col, val in zip(description, row, strict=True)}
        return ResourceRow(
            resource_id=d["resource_id"],
            arena_id=d["arena_id"],
            job_id=d["job_id"],
            lease_id=d["lease_id"],
            generation=d["generation"],
            authority_mode=AuthorityMode(d["authority_mode"]),
            kind=ResourceKind(d["kind"]),
            state=ResourceState(d["state"]),
            retention=Retention(d["retention"]),
            owner_principal=d["owner_principal"],
            quota_domain_id=d["quota_domain_id"],
            logical_bytes=d["logical_bytes"],
            disposition=d["disposition"],
        )
