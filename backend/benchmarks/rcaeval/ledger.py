"""custodian-owned pair ledger for prediction and one-time label opening.

The ledger lives beside the frozen prediction set, never beside an evaluation
output directory. SQLite transactions make reservations atomic. Every
in-flight transition carries a durable lease; a custodian recovery operation
converges an expired lease to FAILED_NON_RESUMABLE before reauthorization.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class LedgerState(StrEnum):
    NEW = "new"
    PREDICTING = "predicting"
    PREDICTIONS_FROZEN = "predictions_frozen"
    PRELABEL_FROZEN = "prelabel_frozen"
    LABELS_OPEN = "labels_open"
    COMPLETED = "completed"
    FAILED_NON_RESUMABLE = "failed_non_resumable"


@dataclass(frozen=True, slots=True)
class PrelabelReservation:
    state: LedgerState
    reservation_token: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True, slots=True)
class LabelOpenReservation:
    state: LedgerState
    label_open_count: int
    lease_token: str
    lease_expires_at: float


class CustodianPairLedger:
    """One immutable benchmark pair, shared by every output directory."""

    DEFAULT_LEASE_SECONDS = 900
    _IN_FLIGHT = {
        LedgerState.PREDICTING,
        LedgerState.PRELABEL_FROZEN,
        LedgerState.LABELS_OPEN,
    }

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(
        self,
        *,
        partition: str,
        prediction_set_hash: str,
        expected_sides: tuple[str, ...],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        if not expected_sides or len(set(expected_sides)) != len(expected_sides):
            raise ValueError("pair ledger requires unique expected sides")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError(
                "pair ledger lease_seconds must be between one second and one day"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pair_ledger (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    partition TEXT NOT NULL,
                    prediction_set_hash TEXT NOT NULL,
                    expected_sides TEXT NOT NULL,
                    state TEXT NOT NULL,
                    prediction_set_bound INTEGER NOT NULL DEFAULT 0,
                    audit_export_hash TEXT,
                    manual_audit_hash TEXT,
                    prelabel_reservation_hash TEXT,
                    label_open_count INTEGER NOT NULL DEFAULT 0,
                    evaluation_artifact_hash TEXT,
                    failure_reason TEXT,
                    reauthorization_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_seconds INTEGER NOT NULL,
                    lease_kind TEXT,
                    lease_token_hash TEXT,
                    lease_side TEXT,
                    lease_expires_at REAL
                )
                """
            )
            required_columns = {
                "lease_seconds",
                "lease_kind",
                "lease_token_hash",
                "lease_side",
                "lease_expires_at",
            }
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(pair_ledger)")
            }
            if not required_columns <= columns:
                raise ValueError("pair ledger schema lacks custodian lease columns")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pair_sides (
                    side TEXT PRIMARY KEY,
                    output_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bundle_hash TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reauthorizations (
                    epoch INTEGER PRIMARY KEY,
                    token_hash TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT partition, prediction_set_hash, expected_sides, lease_seconds "
                "FROM pair_ledger WHERE id = 1"
            ).fetchone()
            expected = _encode_sides(expected_sides)
            if row is None:
                connection.execute(
                    "INSERT INTO pair_ledger("
                    "id, partition, prediction_set_hash, expected_sides, state, lease_seconds"
                    ") VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        partition,
                        prediction_set_hash,
                        expected,
                        LedgerState.NEW.value,
                        lease_seconds,
                    ),
                )
            elif tuple(row) != (
                partition,
                prediction_set_hash,
                expected,
                lease_seconds,
            ):
                raise ValueError(
                    "pair ledger identity or lease differs from custodian freeze"
                )
            connection.commit()

    def bind_prediction_set_hash(self, prediction_set_hash: str) -> None:
        """Bind the exact frozen prediction-root hash once both sides exist."""
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            if LedgerState(row["state"]) != LedgerState.PREDICTIONS_FROZEN:
                raise ValueError("prediction set is not frozen by the custodian")
            if row["prediction_set_bound"]:
                if row["prediction_set_hash"] != prediction_set_hash:
                    raise ValueError("prediction set hash differs from custodian freeze")
                connection.commit()
                return
            connection.execute(
                "UPDATE pair_ledger SET prediction_set_hash = ?, "
                "prediction_set_bound = 1 WHERE id = 1",
                (prediction_set_hash,),
            )
            connection.commit()

    def reserve_evaluation(
        self, *, audit_export_hash: str, manual_audit_hash: str
    ) -> PrelabelReservation:
        """Atomically freeze the exact pre-label audit identity."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            state = LedgerState(row["state"])
            if state != LedgerState.PREDICTIONS_FROZEN:
                raise ValueError(
                    "pre-label audit reservation is already consumed or blocked"
                )
            if not row["prediction_set_bound"]:
                raise ValueError("prediction set hash is not frozen by the custodian")
            if not _valid_hash(audit_export_hash) or not _valid_hash(manual_audit_hash):
                raise ValueError("pre-label audit identity must be sha256")
            token = secrets.token_urlsafe(32)
            expires_at = _now() + float(row["lease_seconds"])
            reservation_hash = _token_hash(token)
            connection.execute(
                "UPDATE pair_ledger SET state = ?, audit_export_hash = ?, "
                "manual_audit_hash = ?, prelabel_reservation_hash = ?, "
                "lease_kind = ?, lease_token_hash = ?, lease_side = NULL, "
                "lease_expires_at = ? WHERE id = 1",
                (
                    LedgerState.PRELABEL_FROZEN.value,
                    audit_export_hash,
                    manual_audit_hash,
                    reservation_hash,
                    "prelabel",
                    reservation_hash,
                    expires_at,
                ),
            )
            connection.commit()
            return PrelabelReservation(
                LedgerState.PRELABEL_FROZEN,
                token,
                token,
                expires_at,
            )

    def reserve_label_open(
        self,
        *,
        audit_export_hash: str,
        manual_audit_hash: str,
        reservation_token: str,
    ) -> LabelOpenReservation:
        """Perform the sole atomic label-open transition."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            state = LedgerState(row["state"])
            if state != LedgerState.PRELABEL_FROZEN:
                raise ValueError(
                    "label open is unavailable: pair is already open, consumed, or non-resumable"
                )
            self._require_lease(
                row,
                token=reservation_token,
                kind="prelabel",
            )
            if (row["audit_export_hash"], row["manual_audit_hash"]) != (
                audit_export_hash,
                manual_audit_hash,
            ):
                raise ValueError("label open audit identity differs from pre-label freeze")
            if (
                not reservation_token.strip()
                or row["prelabel_reservation_hash"] != _token_hash(reservation_token)
            ):
                raise ValueError("label open reservation is invalid or expired")
            if row["label_open_count"] != 0:
                raise ValueError("label open already consumed")
            token = secrets.token_urlsafe(32)
            expires_at = _now() + float(row["lease_seconds"])
            connection.execute(
                "UPDATE pair_ledger SET state = ?, label_open_count = 1, "
                "prelabel_reservation_hash = NULL, lease_kind = ?, "
                "lease_token_hash = ?, lease_side = NULL, lease_expires_at = ? WHERE id = 1",
                (
                    LedgerState.LABELS_OPEN.value,
                    "label_open",
                    _token_hash(token),
                    expires_at,
                ),
            )
            connection.commit()
            return LabelOpenReservation(
                LedgerState.LABELS_OPEN,
                1,
                token,
                expires_at,
            )

    def assert_label_open(
        self,
        *,
        partition: str,
        prediction_set_hash: str,
        audit_export_hash: str,
        manual_audit_hash: str,
        lease_token: str | None = None,
    ) -> None:
        """Read-only child-process fence immediately before labels are read."""
        with self._connection() as connection:
            row = self._require_pair(connection)
            if LedgerState(row["state"]) != LedgerState.LABELS_OPEN:
                raise ValueError(
                    "custodian ledger labels are closed; reauthorization is required"
                )
            self._require_lease(row, token=lease_token, kind="label_open")
            if (row["partition"], row["prediction_set_hash"]) != (
                partition,
                prediction_set_hash,
            ):
                raise ValueError("custodian ledger prediction pair identity mismatch")
            if row["label_open_count"] != 1:
                raise ValueError("custodian ledger label-open count is invalid")
            if (row["audit_export_hash"], row["manual_audit_hash"]) != (
                audit_export_hash,
                manual_audit_hash,
            ):
                raise ValueError("custodian ledger audit identity mismatch")

    def heartbeat(
        self, lease_token: str, *, now: float | datetime | None = None
    ) -> float:
        """Extend one live custodian lease; stale holders cannot revive it."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            current = _coerce_now(now)
            if LedgerState(row["state"]) not in self._IN_FLIGHT:
                raise ValueError("pair has no live in-flight lease")
            self._require_lease(
                row,
                token=lease_token,
                kind=row["lease_kind"],
                now=current,
            )
            expires_at = current + float(row["lease_seconds"])
            connection.execute(
                "UPDATE pair_ledger SET lease_expires_at = ? WHERE id = 1",
                (expires_at,),
            )
            connection.commit()
            return expires_at

    def mark_evaluation_completed(
        self, evaluation_artifact_hash: str, *, lease_token: str | None = None
    ) -> None:
        if not _valid_hash(evaluation_artifact_hash):
            raise ValueError("evaluation artifact hash must be sha256")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            if LedgerState(row["state"]) != LedgerState.LABELS_OPEN:
                raise ValueError("pair is not open for evaluation completion")
            self._require_lease(row, token=lease_token, kind="label_open")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, evaluation_artifact_hash = ?, "
                "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
                "lease_expires_at = NULL WHERE id = 1",
                (LedgerState.COMPLETED.value, evaluation_artifact_hash),
            )
            connection.commit()

    def record_side_started(self, side: str, output_dir: str) -> str:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            state = LedgerState(row["state"])
            if state not in {LedgerState.NEW, LedgerState.PREDICTING}:
                raise ValueError("pair is blocked; explicit reauthorization is required")
            in_flight = connection.execute(
                "SELECT side FROM pair_sides WHERE status = 'started' LIMIT 1"
            ).fetchone()
            if in_flight is not None:
                raise ValueError(
                    "pair has an in-flight prediction side; reauthorization is required"
                )
            expected = set(_decode_sides(row["expected_sides"]))
            if side not in expected:
                raise ValueError("prediction side is not part of the frozen pair")
            existing = connection.execute(
                "SELECT output_dir, status FROM pair_sides WHERE side = ?", (side,)
            ).fetchone()
            if existing is not None:
                raise ValueError("prediction side already attempted; pair is non-resumable")
            token = secrets.token_urlsafe(32)
            expires_at = _now() + float(row["lease_seconds"])
            connection.execute(
                "INSERT INTO pair_sides(side, output_dir, status) VALUES (?, ?, 'started')",
                (side, str(Path(output_dir).resolve())),
            )
            connection.execute(
                "UPDATE pair_ledger SET state = ?, lease_kind = ?, "
                "lease_token_hash = ?, lease_side = ?, lease_expires_at = ? WHERE id = 1",
                (
                    LedgerState.PREDICTING.value,
                    "prediction",
                    _token_hash(token),
                    side,
                    expires_at,
                ),
            )
            connection.commit()
            return token

    def record_side_completed(
        self, side: str, bundle_hash: str, *, lease_token: str | None = None
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            if LedgerState(row["state"]) != LedgerState.PREDICTING:
                raise ValueError("pair is not accepting prediction completions")
            self._require_lease(
                row,
                token=lease_token,
                kind="prediction",
                side=side,
            )
            if not _valid_hash(bundle_hash):
                raise ValueError("prediction bundle hash must be sha256")
            current = connection.execute(
                "SELECT status FROM pair_sides WHERE side = ?", (side,)
            ).fetchone()
            if current is None or current["status"] != "started":
                raise ValueError("prediction side was not atomically started")
            connection.execute(
                "UPDATE pair_sides SET status = 'completed', bundle_hash = ? WHERE side = ?",
                (bundle_hash, side),
            )
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM pair_sides WHERE status = 'completed'"
            ).fetchone()["count"]
            expected = len(_decode_sides(row["expected_sides"]))
            connection.execute(
                "UPDATE pair_ledger SET state = ?, lease_kind = NULL, "
                "lease_token_hash = NULL, lease_side = NULL, lease_expires_at = NULL WHERE id = 1",
                (
                    LedgerState.PREDICTIONS_FROZEN.value
                    if total == expected
                    else LedgerState.PREDICTING.value,
                ),
            )
            connection.commit()

    def invalidate_pair(self, reason: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_pair(connection)
            self._fail_pair(connection, reason)
            connection.commit()

    def recover_expired(
        self, *, now: float | datetime | None = None
    ) -> LedgerState:
        """Custodian-only recovery for a lost process/lease.

        The transition is pair-level and atomic: both sides become invalidated,
        label reservations remain consumed, and only explicit reauthorization
        can start a new pair.
        """
        current = _coerce_now(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=current)
            connection.commit()
            return LedgerState(row["state"])

    def reauthorize(self, owner_token: str) -> None:
        if not owner_token.strip():
            raise ValueError("explicit non-empty reauthorization is required")
        token_hash = _token_hash(owner_token)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            if LedgerState(row["state"]) != LedgerState.FAILED_NON_RESUMABLE:
                raise ValueError("reauthorization is only valid for a failed pair")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, prediction_set_bound = 0, "
                "prelabel_reservation_hash = NULL, audit_export_hash = NULL, "
                "manual_audit_hash = NULL, label_open_count = 0, "
                "evaluation_artifact_hash = NULL, failure_reason = NULL, "
                "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
                "lease_expires_at = NULL, reauthorization_epoch = reauthorization_epoch + 1 "
                "WHERE id = 1",
                (LedgerState.NEW.value,),
            )
            connection.execute("DELETE FROM pair_sides")
            connection.execute(
                "INSERT INTO reauthorizations(epoch, token_hash) VALUES (?, ?)",
                (row["reauthorization_epoch"] + 1, token_hash),
            )
            connection.commit()

    def snapshot(self) -> dict[str, object]:
        with self._connection() as connection:
            row = self._require_pair(connection)
            return dict(row)

    def side_snapshot(self) -> dict[str, str]:
        with self._connection() as connection:
            self._require_pair(connection)
            rows = connection.execute(
                "SELECT side, status FROM pair_sides ORDER BY side"
            ).fetchall()
            return {row["side"]: row["status"] for row in rows}

    def _recover_if_expired(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, now: float
    ) -> sqlite3.Row:
        state = LedgerState(row["state"])
        if state not in self._IN_FLIGHT:
            return row
        if state == LedgerState.PREDICTING:
            active_side = connection.execute(
                "SELECT 1 FROM pair_sides WHERE status = 'started' LIMIT 1"
            ).fetchone()
            if active_side is None:
                return row
        expires_at = row["lease_expires_at"]
        if expires_at is not None and float(expires_at) > now:
            return row
        self._fail_pair(connection, "custodian lease expired; pair is non-resumable")
        return self._require_pair(connection)

    def _fail_pair(self, connection: sqlite3.Connection, reason: str) -> None:
        connection.execute(
            "UPDATE pair_ledger SET state = ?, failure_reason = ?, "
            "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
            "lease_expires_at = NULL WHERE id = 1",
            (LedgerState.FAILED_NON_RESUMABLE.value, reason[:256]),
        )
        connection.execute("UPDATE pair_sides SET status = 'invalidated'")

    @staticmethod
    def _require_lease(
        row: sqlite3.Row,
        *,
        token: str | None,
        kind: str | None,
        side: str | None = None,
        now: float | None = None,
    ) -> None:
        if row["lease_kind"] != kind:
            raise ValueError("custodian lease kind is invalid")
        expires_at = row["lease_expires_at"]
        if expires_at is None or float(expires_at) <= (_now() if now is None else now):
            raise ValueError("custodian lease expired; recovery/reauthorization is required")
        if side is not None and row["lease_side"] != side:
            raise ValueError("custodian lease side is invalid")
        if not token or _token_hash(token) != row["lease_token_hash"]:
            raise ValueError("custodian lease token/reservation is invalid")

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _require_pair(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM pair_ledger WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("pair ledger has not been initialized by the custodian")
        return row


def _encode_sides(sides: tuple[str, ...]) -> str:
    return "\n".join(sorted(sides))


def _decode_sides(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split("\n") if item)


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def _coerce_now(value: float | datetime | None) -> float:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)
