"""custodian-owned pair ledger for prediction and one-time label opening.

The ledger lives beside the frozen prediction set, never beside an evaluation
output directory. SQLite transactions make reservations atomic. Every
in-flight transition carries a durable lease; a custodian recovery operation
converges an expired lease to FAILED_NON_RESUMABLE before reauthorization.
"""

from __future__ import annotations

import hashlib
import json
import os
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


@dataclass(frozen=True, slots=True)
class CustodianRootManifest:
    """Immutable anchor owned by the custodian, not by an output directory."""

    path: Path
    canonical_root: str
    runtime_manifest_hash: str
    label_manifest_hash: str
    ledger_filename: str
    manifest_hash: str


def create_custodian_manifest(
    canonical_root: Path,
    *,
    runtime_manifest_hash: str,
    label_manifest_hash: str,
) -> Path:
    """Create or verify the one canonical root anchor for a prepared dataset."""
    if not _valid_hash(runtime_manifest_hash) or not _valid_hash(label_manifest_hash):
        raise ValueError("custodian manifest identities must be sha256")
    root = _canonical_path(canonical_root)
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    path = root_path / "custodian-manifest.json"
    payload = {
        "schema_version": "rcaeval-custodian-root-v1",
        "canonical_root": root,
        "runtime_manifest_hash": runtime_manifest_hash,
        "label_manifest_hash": label_manifest_hash,
        "ledger_filename": "pair-ledger.sqlite3",
    }
    sealed = dict(payload, manifest_hash=_canonical_hash(payload))
    encoded = _canonical_json(sealed)
    try:
        with path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        existing = _load_custodian_manifest(path)
        if existing.manifest_hash != sealed["manifest_hash"]:
            raise ValueError(
                "custodian manifest already exists with a different identity"
            ) from exc
    return path


class CustodianPairLedger:
    """One immutable benchmark pair, shared by every output directory."""

    DEFAULT_LEASE_SECONDS = 900
    _IN_FLIGHT = {
        LedgerState.PREDICTING,
        LedgerState.PRELABEL_FROZEN,
        LedgerState.LABELS_OPEN,
    }

    def __init__(
        self,
        path: Path,
        *,
        custodian_manifest: Path | None = None,
    ) -> None:
        self.path = Path(path).resolve()
        self._manifest = (
            _load_custodian_manifest(custodian_manifest)
            if custodian_manifest is not None
            else None
        )
        if self._manifest is not None:
            expected_path = _canonical_path(
                Path(self._manifest.canonical_root) / self._manifest.ledger_filename
            )
            if _canonical_path(self.path) != expected_path:
                raise ValueError("ledger path is outside the canonical custodian root")

    @classmethod
    def from_manifest(cls, manifest_path: Path) -> CustodianPairLedger:
        manifest = _load_custodian_manifest(manifest_path)
        return cls(
            Path(manifest.canonical_root) / manifest.ledger_filename,
            custodian_manifest=manifest.path,
        )

    @property
    def canonical_root(self) -> Path:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return Path(self._manifest.canonical_root)

    @property
    def custodian_manifest_hash(self) -> str:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return self._manifest.manifest_hash

    @property
    def runtime_manifest_hash(self) -> str:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return self._manifest.runtime_manifest_hash

    @property
    def label_manifest_hash(self) -> str:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return self._manifest.label_manifest_hash

    def initialize(
        self,
        *,
        partition: str,
        prediction_set_hash: str,
        expected_sides: tuple[str, ...],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if self._manifest is None:
            raise ValueError("custodian manifest is required to initialize pair ledger")
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        if not expected_sides or len(set(expected_sides)) != len(expected_sides):
            raise ValueError("pair ledger requires unique expected sides")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError(
                "pair ledger lease_seconds must be between one second and one day"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        canonical_root = self._manifest.canonical_root
        ledger_identity = _ledger_identity(
            canonical_root=canonical_root,
            manifest_hash=self._manifest.manifest_hash,
            partition=partition,
            prediction_set_hash=prediction_set_hash,
            expected_sides=expected_sides,
        )
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
                    lease_expires_at REAL,
                    custodian_root TEXT,
                    custodian_manifest_hash TEXT,
                    ledger_identity TEXT,
                    label_ever_opened INTEGER NOT NULL DEFAULT 0,
                    reveal_epoch INTEGER NOT NULL DEFAULT 0,
                    label_lineage_hash TEXT,
                    authorized_evaluation_identity_hash TEXT
                )
                """
            )
            required_columns = {
                "lease_seconds",
                "lease_kind",
                "lease_token_hash",
                "lease_side",
                "lease_expires_at",
                "custodian_root",
                "custodian_manifest_hash",
                "ledger_identity",
                "label_ever_opened",
                "reveal_epoch",
                "label_lineage_hash",
                "authorized_evaluation_identity_hash",
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
                    token_hash TEXT NOT NULL UNIQUE,
                    evaluation_identity_hash TEXT NOT NULL
                )
                """
            )
            reauthorization_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(reauthorizations)")
            }
            if not {"epoch", "token_hash", "evaluation_identity_hash"} <= reauthorization_columns:
                raise ValueError("reauthorization schema is not custodian-frozen")
            row = connection.execute(
                "SELECT partition, prediction_set_hash, expected_sides, lease_seconds, "
                "custodian_root, custodian_manifest_hash, ledger_identity "
                "FROM pair_ledger WHERE id = 1"
            ).fetchone()
            expected = _encode_sides(expected_sides)
            if row is None:
                connection.execute(
                    "INSERT INTO pair_ledger("
                    "id, partition, prediction_set_hash, expected_sides, state, lease_seconds, "
                    "custodian_root, custodian_manifest_hash, ledger_identity, "
                    "authorized_evaluation_identity_hash"
                    ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        partition,
                        prediction_set_hash,
                        expected,
                        LedgerState.NEW.value,
                        lease_seconds,
                        canonical_root,
                        self._manifest.manifest_hash,
                        ledger_identity,
                        ledger_identity,
                    ),
                )
            elif tuple(row) != (
                partition,
                prediction_set_hash,
                expected,
                lease_seconds,
                canonical_root,
                self._manifest.manifest_hash,
                ledger_identity,
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
            if row["label_ever_opened"]:
                raise ValueError("label lineage was already revealed and is consumed")
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
            if row["label_open_count"] != 0 or row["label_ever_opened"]:
                raise ValueError("label open already consumed")
            token = secrets.token_urlsafe(32)
            expires_at = _now() + float(row["lease_seconds"])
            lineage_hash = _lineage_hash(audit_export_hash, manual_audit_hash)
            connection.execute(
                "UPDATE pair_ledger SET state = ?, label_open_count = 1, "
                "label_ever_opened = 1, reveal_epoch = reveal_epoch + 1, "
                "label_lineage_hash = ?, "
                "prelabel_reservation_hash = NULL, lease_kind = ?, "
                "lease_token_hash = ?, lease_side = NULL, lease_expires_at = ? WHERE id = 1",
                (
                    LedgerState.LABELS_OPEN.value,
                    lineage_hash,
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
        lease_token: str,
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
        self, evaluation_artifact_hash: str, *, lease_token: str
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
        self, side: str, bundle_hash: str, *, lease_token: str
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

    def reauthorize(
        self, owner_token: str, *, authorized_evaluation_identity: str
    ) -> None:
        if not owner_token.strip():
            raise ValueError("explicit non-empty reauthorization is required")
        if not _valid_hash(authorized_evaluation_identity):
            raise ValueError("authorized evaluation identity must be sha256")
        token_hash = _token_hash(owner_token)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            if LedgerState(row["state"]) != LedgerState.FAILED_NON_RESUMABLE:
                raise ValueError("reauthorization is only valid for a failed pair")
            if row["label_ever_opened"]:
                raise ValueError("label lineage was already revealed; reauthorization is forbidden")
            used = connection.execute(
                "SELECT 1 FROM reauthorizations WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if used is not None:
                raise ValueError("reauthorization token was already consumed")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, prediction_set_bound = 0, "
                "prelabel_reservation_hash = NULL, audit_export_hash = NULL, "
                "manual_audit_hash = NULL, label_open_count = 0, "
                "label_lineage_hash = NULL, "
                "authorized_evaluation_identity_hash = ?, "
                "evaluation_artifact_hash = NULL, failure_reason = NULL, "
                "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
                "lease_expires_at = NULL, reauthorization_epoch = reauthorization_epoch + 1 "
                "WHERE id = 1",
                (LedgerState.NEW.value, authorized_evaluation_identity),
            )
            connection.execute("DELETE FROM pair_sides")
            connection.execute(
                "INSERT INTO reauthorizations(epoch, token_hash, evaluation_identity_hash) "
                "VALUES (?, ?, ?)",
                (row["reauthorization_epoch"] + 1, token_hash, authorized_evaluation_identity),
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
        token: str,
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
        if self._manifest is None:
            raise ValueError("custodian manifest is required for pair ledger access")
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


def _canonical_path(path: Path) -> str:
    """Normalize symlinks, junctions, separators, and case before identity use."""
    return os.path.normcase(os.path.normpath(str(Path(path).expanduser().resolve(strict=False))))


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _canonical_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).rstrip("\n").encode("utf-8")).hexdigest()


def _load_custodian_manifest(path: Path) -> CustodianRootManifest:
    manifest_path = Path(path).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("custodian manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("custodian manifest must be an object")
    required = {
        "schema_version",
        "canonical_root",
        "runtime_manifest_hash",
        "label_manifest_hash",
        "ledger_filename",
        "manifest_hash",
    }
    if set(payload) != required:
        raise ValueError("custodian manifest schema is not frozen")
    if payload["schema_version"] != "rcaeval-custodian-root-v1":
        raise ValueError("custodian manifest schema is not frozen")
    root = _canonical_path(Path(str(payload["canonical_root"])))
    if root != str(payload["canonical_root"]):
        raise ValueError("custodian manifest canonical root is not normalized")
    if (
        _canonical_path(manifest_path.parent) != root
        or manifest_path.name != "custodian-manifest.json"
    ):
        raise ValueError("custodian manifest is not at its canonical custodian root")
    if payload["ledger_filename"] != "pair-ledger.sqlite3":
        raise ValueError("custodian ledger filename is not frozen")
    if not _valid_hash(str(payload["runtime_manifest_hash"])) or not _valid_hash(
        str(payload["label_manifest_hash"])
    ):
        raise ValueError("custodian manifest identities must be sha256")
    identity = {key: payload[key] for key in required if key != "manifest_hash"}
    if _canonical_hash(identity) != payload["manifest_hash"]:
        raise ValueError("custodian manifest hash mismatch")
    return CustodianRootManifest(
        path=manifest_path,
        canonical_root=root,
        runtime_manifest_hash=str(payload["runtime_manifest_hash"]),
        label_manifest_hash=str(payload["label_manifest_hash"]),
        ledger_filename=str(payload["ledger_filename"]),
        manifest_hash=str(payload["manifest_hash"]),
    )


def _ledger_identity(
    *,
    canonical_root: str,
    manifest_hash: str,
    partition: str,
    prediction_set_hash: str,
    expected_sides: tuple[str, ...],
) -> str:
    payload = {
        "canonical_root": canonical_root,
        "custodian_manifest_hash": manifest_hash,
        "partition": partition,
        "prediction_set_hash": prediction_set_hash,
        "expected_sides": sorted(expected_sides),
    }
    return _canonical_hash(payload)


def _lineage_hash(audit_export_hash: str, manual_audit_hash: str) -> str:
    return _canonical_hash(
        {
            "audit_export_hash": audit_export_hash,
            "manual_audit_hash": manual_audit_hash,
        }
    )


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
