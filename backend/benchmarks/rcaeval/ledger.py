"""custodian-owned pair ledger for prediction and one-time label opening.

The ledger lives beside the frozen prediction set, never beside an evaluation
output directory.  SQLite transactions make reservations atomic; an
interrupted reservation is deliberately left in a blocking state and cannot
be resumed implicitly.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class LabelOpenReservation:
    state: LedgerState
    label_open_count: int


class CustodianPairLedger:
    """One immutable benchmark pair, shared by every output directory."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(
        self,
        *,
        partition: str,
        prediction_set_hash: str,
        expected_sides: tuple[str, ...],
    ) -> None:
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        if not expected_sides or len(set(expected_sides)) != len(expected_sides):
            raise ValueError("pair ledger requires unique expected sides")
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
                    reauthorization_epoch INTEGER NOT NULL DEFAULT 0
                )
                """
            )
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
            row = connection.execute(
                "SELECT partition, prediction_set_hash, expected_sides "
                "FROM pair_ledger WHERE id = 1"
            ).fetchone()
            expected = _encode_sides(expected_sides)
            if row is None:
                connection.execute(
                    "INSERT INTO pair_ledger("
                    "id, partition, prediction_set_hash, expected_sides, state"
                    ") VALUES (1, ?, ?, ?, ?)",
                    (partition, prediction_set_hash, expected, LedgerState.NEW.value),
                )
            elif tuple(row) != (partition, prediction_set_hash, expected):
                raise ValueError("pair ledger identity differs from custodian freeze")
            connection.commit()

    def bind_prediction_set_hash(self, prediction_set_hash: str) -> None:
        """Bind the exact frozen prediction-root hash once both sides exist."""
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
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
            reservation_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            connection.execute(
                "UPDATE pair_ledger SET state = ?, audit_export_hash = ?, "
                "manual_audit_hash = ?, prelabel_reservation_hash = ? "
                "WHERE id = 1",
                (
                    LedgerState.PRELABEL_FROZEN.value,
                    audit_export_hash,
                    manual_audit_hash,
                    reservation_hash,
                ),
            )
            connection.commit()
            return PrelabelReservation(LedgerState.PRELABEL_FROZEN, token)

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
            state = LedgerState(row["state"])
            if state != LedgerState.PRELABEL_FROZEN:
                raise ValueError(
                    "label open is unavailable: pair is already open, consumed, or non-resumable"
                )
            if (row["audit_export_hash"], row["manual_audit_hash"]) != (
                audit_export_hash,
                manual_audit_hash,
            ):
                raise ValueError("label open audit identity differs from pre-label freeze")
            if not reservation_token.strip() or row["prelabel_reservation_hash"] != hashlib.sha256(
                reservation_token.encode("utf-8")
            ).hexdigest():
                raise ValueError("label open reservation is invalid or expired")
            if row["label_open_count"] != 0:
                raise ValueError("label open already consumed")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, label_open_count = 1, "
                "prelabel_reservation_hash = NULL WHERE id = 1",
                (LedgerState.LABELS_OPEN.value,),
            )
            connection.commit()
            return LabelOpenReservation(LedgerState.LABELS_OPEN, 1)

    def assert_label_open(
        self,
        *,
        partition: str,
        prediction_set_hash: str,
        audit_export_hash: str,
        manual_audit_hash: str,
    ) -> None:
        """Read-only child-process fence immediately before labels are read."""
        with self._connection() as connection:
            row = self._require_pair(connection)
            if LedgerState(row["state"]) != LedgerState.LABELS_OPEN:
                raise ValueError("custodian ledger has not atomically opened labels")
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

    def mark_evaluation_completed(self, evaluation_artifact_hash: str) -> None:
        if not _valid_hash(evaluation_artifact_hash):
            raise ValueError("evaluation artifact hash must be sha256")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            if LedgerState(row["state"]) != LedgerState.LABELS_OPEN:
                raise ValueError("pair is not open for evaluation completion")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, evaluation_artifact_hash = ? WHERE id = 1",
                (LedgerState.COMPLETED.value, evaluation_artifact_hash),
            )
            connection.commit()

    def record_side_started(self, side: str, output_dir: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            state = LedgerState(row["state"])
            if state not in {LedgerState.NEW, LedgerState.PREDICTING}:
                raise ValueError("pair is blocked; explicit reauthorization is required")
            in_flight = connection.execute(
                "SELECT side FROM pair_sides WHERE status = 'started' LIMIT 1"
            ).fetchone()
            if in_flight is not None:
                raise ValueError(
                    "pair has an in-flight prediction side; "
                    "reauthorization is required"
                )
            expected = set(_decode_sides(row["expected_sides"]))
            if side not in expected:
                raise ValueError("prediction side is not part of the frozen pair")
            existing = connection.execute(
                "SELECT output_dir, status FROM pair_sides WHERE side = ?", (side,)
            ).fetchone()
            if existing is not None:
                raise ValueError("prediction side already attempted; pair is non-resumable")
            connection.execute(
                "INSERT INTO pair_sides(side, output_dir, status) VALUES (?, ?, 'started')",
                (side, str(Path(output_dir).resolve())),
            )
            connection.execute(
                "UPDATE pair_ledger SET state = ? WHERE id = 1",
                (LedgerState.PREDICTING.value,),
            )
            connection.commit()

    def record_side_completed(self, side: str, bundle_hash: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            if LedgerState(row["state"]) not in {LedgerState.PREDICTING}:
                raise ValueError("pair is not accepting prediction completions")
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
            if total == expected:
                connection.execute(
                    "UPDATE pair_ledger SET state = ? WHERE id = 1",
                    (LedgerState.PREDICTIONS_FROZEN.value,),
                )
            connection.commit()

    def invalidate_pair(self, reason: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_pair(connection)
            connection.execute(
                "UPDATE pair_ledger SET state = ?, failure_reason = ? WHERE id = 1",
                (LedgerState.FAILED_NON_RESUMABLE.value, reason[:256]),
            )
            connection.execute("UPDATE pair_sides SET status = 'invalidated'")
            connection.commit()

    def reauthorize(self, owner_token: str) -> None:
        if not owner_token.strip():
            raise ValueError("explicit non-empty reauthorization is required")
        token_hash = hashlib.sha256(owner_token.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            if LedgerState(row["state"]) != LedgerState.FAILED_NON_RESUMABLE:
                raise ValueError("reauthorization is only valid for a failed pair")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, prediction_set_bound = 0, "
                "prelabel_reservation_hash = NULL, failure_reason = NULL, "
                "reauthorization_epoch = reauthorization_epoch + 1 WHERE id = 1",
                (LedgerState.NEW.value,),
            )
            connection.execute("DELETE FROM pair_sides")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reauthorizations ("
                "epoch INTEGER PRIMARY KEY, token_hash TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO reauthorizations(epoch, token_hash) VALUES (?, ?)",
                (row["reauthorization_epoch"] + 1, token_hash),
            )
            connection.commit()

    def snapshot(self) -> dict[str, object]:
        with self._connection() as connection:
            row = self._require_pair(connection)
            return dict(row)

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
