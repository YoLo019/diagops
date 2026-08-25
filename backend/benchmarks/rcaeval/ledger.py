"""custodian-owned pair ledger for prediction and one-time label opening.

The ledger lives beside the frozen prediction set, never beside an evaluation
output directory. SQLite transactions make reservations atomic. Every
in-flight transition carries a durable lease; a custodian recovery operation
converges an expired lease to FAILED_NON_RESUMABLE before reauthorization.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from backend.benchmarks.rcaeval.models import canonical_json_sha256
from backend.services.source_identity import reject_reparse_path


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


SEAL_KEY_FILENAME = "pair-ledger-seal.key"
SEAL_ANCHOR_FILENAME = "pair-ledger-seal.anchor"
RUNTIME_VERIFICATION_DIRNAME = ".runtime-verification"


def create_custodian_manifest(
    canonical_root: Path,
    *,
    runtime_manifest_hash: str,
    label_manifest_hash: str,
) -> Path:
    """Create or verify the one canonical root anchor for a prepared dataset."""
    if not _valid_hash(runtime_manifest_hash) or not _valid_hash(label_manifest_hash):
        raise ValueError("custodian manifest identities must be sha256")
    raw_root = Path(canonical_root).expanduser()
    # H5: 任何 resolve/open 前先拒绝 root、父目录与固定文件的 reparse/symlink。
    reject_reparse_path(raw_root, "custodian root")
    root_path = raw_root.resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    # M2: manifest 绑定最终句柄拼写；别名/junction/小写盘符一律拒绝。
    root = canonical_locator(root_path)
    root_path = Path(root)
    path = root_path / "custodian-manifest.json"
    payload = {
        "schema_version": "rcaeval-custodian-root-v1",
        "canonical_root": root,
        "runtime_manifest_hash": runtime_manifest_hash,
        "label_manifest_hash": label_manifest_hash,
        "ledger_filename": "pair-ledger.sqlite3",
    }
    sealed = dict(payload, manifest_hash=canonical_json_sha256(payload))
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
    _ensure_seal_key(root_path)
    return path


def _ensure_seal_key(root_path: Path) -> Path:
    """Create the custodian-owned HMAC key once; never overwrite it."""
    path = root_path / SEAL_KEY_FILENAME
    try:
        with path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(secrets.token_hex(32) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except FileExistsError:
        _read_seal_key(path)
    return path


def _read_seal_key(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("custodian seal key is unreadable") from exc
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("custodian seal key is invalid")
    return bytes.fromhex(text)


def _guarded_write(reason: str):
    """写边界统一守卫：锁/异常/重试耗尽时持久化可验证 failure intent。"""

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self: CustodianPairLedger, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                self._invalidate_after_write_error(reason, exc)
                raise

        return wrapper

    return decorator


class CustodianPairLedger:
    """One immutable benchmark pair, shared by every output directory."""

    DEFAULT_LEASE_SECONDS = 900
    SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
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
            self._seal_key = _read_seal_key(
                Path(self._manifest.canonical_root) / SEAL_KEY_FILENAME
            )
        else:
            self._seal_key = None

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

    @property
    def failure_intent_path(self) -> Path:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return self.canonical_root / "pair-ledger-failure-intent.json"

    @property
    def seal_key_path(self) -> Path:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return self.canonical_root / SEAL_KEY_FILENAME

    @property
    def seal_anchor_path(self) -> Path:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        return self.canonical_root / SEAL_ANCHOR_FILENAME

    @_guarded_write("pair ledger initialization failed or was interrupted")
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
            lineage_identity_hash=prediction_set_hash,
        )
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
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
                    lineage_identity_hash TEXT,
                    label_ever_opened INTEGER NOT NULL DEFAULT 0,
                    reveal_epoch INTEGER NOT NULL DEFAULT 0,
                    label_lineage_hash TEXT,
                    authorized_evaluation_identity_hash TEXT,
                    freeze_intent_hash TEXT,
                    freeze_intent_locator TEXT
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
                "lineage_identity_hash",
                "label_ever_opened",
                "reveal_epoch",
                "label_lineage_hash",
                "authorized_evaluation_identity_hash",
                "freeze_intent_hash",
                "freeze_intent_locator",
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
                    output_locator TEXT NOT NULL,
                    output_volume TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bundle_hash TEXT
                )
                """
            )
            side_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pair_sides)")
            }
            required_side_columns = {
                "side",
                "output_dir",
                "output_locator",
                "output_volume",
                "status",
                "bundle_hash",
            }
            if not required_side_columns <= side_columns:
                raise ValueError("pair side schema is not custodian-frozen")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_kind TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                )
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS ledger_events_no_update
                BEFORE UPDATE ON ledger_events
                BEGIN SELECT RAISE(ABORT, 'ledger event history is append-only'); END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS ledger_events_no_delete
                BEFORE DELETE ON ledger_events
                BEGIN SELECT RAISE(ABORT, 'ledger event history is append-only'); END
                """
            )
            row = connection.execute(
                "SELECT partition, prediction_set_hash, expected_sides, lease_seconds, "
                "custodian_root, custodian_manifest_hash, ledger_identity, "
                "lineage_identity_hash, authorized_evaluation_identity_hash "
                "FROM pair_ledger WHERE id = 1"
            ).fetchone()
            expected = _encode_sides(expected_sides)
            if row is None:
                connection.execute(
                    "INSERT INTO pair_ledger("
                    "id, partition, prediction_set_hash, expected_sides, state, lease_seconds, "
                    "custodian_root, custodian_manifest_hash, ledger_identity, "
                    "lineage_identity_hash, authorized_evaluation_identity_hash"
                    ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        partition,
                        prediction_set_hash,
                        expected,
                        LedgerState.NEW.value,
                        lease_seconds,
                        canonical_root,
                        self._manifest.manifest_hash,
                        ledger_identity,
                        prediction_set_hash,
                        prediction_set_hash,
                    ),
                )
            else:
                self._assert_sealed(connection)
                lineage_identity_hash = row[7]
                expected_identity = _ledger_identity(
                    canonical_root=canonical_root,
                    manifest_hash=self._manifest.manifest_hash,
                    partition=partition,
                    prediction_set_hash=prediction_set_hash,
                    expected_sides=expected_sides,
                    lineage_identity_hash=lineage_identity_hash,
                )
                if tuple(row) != (
                    partition,
                    prediction_set_hash,
                    expected,
                    lease_seconds,
                    canonical_root,
                    self._manifest.manifest_hash,
                    expected_identity,
                    lineage_identity_hash,
                    prediction_set_hash,
                ):
                    raise ValueError(
                        "pair ledger identity or authorized lineage differs from custodian freeze"
                    )
            self._commit(connection, "initialize")

    @_guarded_write("prediction set binding failed or was interrupted")
    def bind_prediction_set_hash(self, prediction_set_hash: str) -> None:
        """Bind the exact frozen prediction-root hash once both sides exist."""
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
            if LedgerState(row["state"]) != LedgerState.PREDICTIONS_FROZEN:
                raise ValueError("prediction set is not frozen by the custodian")
            if row["prediction_set_bound"]:
                if row["prediction_set_hash"] != prediction_set_hash:
                    raise ValueError("prediction set hash differs from custodian freeze")
                expected_identity = _ledger_identity(
                    canonical_root=self._manifest.canonical_root,
                    manifest_hash=self._manifest.manifest_hash,
                    partition=row["partition"],
                    prediction_set_hash=prediction_set_hash,
                    expected_sides=_decode_sides(row["expected_sides"]),
                    lineage_identity_hash=row["lineage_identity_hash"],
                )
                if row["ledger_identity"] != expected_identity:
                    raise ValueError("bound prediction lineage identity is tampered")
                if row["authorized_evaluation_identity_hash"] != prediction_set_hash:
                    raise ValueError("bound evaluation identity is stale")
                if row["freeze_intent_hash"] is not None:
                    raise ValueError("bound prediction set retains an unfinished freeze intent")
                self._commit(connection, "bind_prediction_set_idempotent")
                return
            if row["authorized_evaluation_identity_hash"] != row["prediction_set_hash"]:
                raise ValueError("prediction lineage authorization is stale")
            if row["freeze_intent_hash"] != prediction_set_hash:
                raise ValueError("prediction set hash was not custodian-prepared for binding")
            next_identity = _ledger_identity(
                canonical_root=self._manifest.canonical_root,
                manifest_hash=self._manifest.manifest_hash,
                partition=row["partition"],
                prediction_set_hash=prediction_set_hash,
                expected_sides=_decode_sides(row["expected_sides"]),
                lineage_identity_hash=row["lineage_identity_hash"],
            )
            connection.execute(
                "UPDATE pair_ledger SET prediction_set_hash = ?, "
                "prediction_set_bound = 1, authorized_evaluation_identity_hash = ?, "
                "ledger_identity = ?, freeze_intent_hash = NULL, "
                "freeze_intent_locator = NULL WHERE id = 1",
                (prediction_set_hash, prediction_set_hash, next_identity),
            )
            self._commit(connection, "bind_prediction_set")

    @_guarded_write("prediction freeze intent failed or was interrupted")
    def prepare_prediction_set_freeze(
        self, *, prediction_set_hash: str, root_locator: str
    ) -> None:
        """Persist the freeze intent before materializing the filesystem marker."""
        if not _valid_hash(prediction_set_hash):
            raise ValueError("prediction set identity must be sha256")
        canonical_locator = _canonical_locator(Path(root_locator))
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
            if LedgerState(row["state"]) != LedgerState.PREDICTIONS_FROZEN:
                raise ValueError("prediction set is not frozen by the custodian")
            if row["prediction_set_bound"]:
                if row["prediction_set_hash"] != prediction_set_hash:
                    raise ValueError("bound prediction set identity differs")
                if row["freeze_intent_hash"] is not None:
                    raise ValueError("bound prediction set has an unfinished freeze intent")
                self._commit(connection, "prepare_freeze_idempotent")
                return
            if row["freeze_intent_hash"] is not None:
                if (
                    row["freeze_intent_hash"] != prediction_set_hash
                    or row["freeze_intent_locator"] != canonical_locator
                ):
                    raise ValueError("prediction freeze intent differs from custodian freeze")
                self._commit(connection, "prepare_freeze_idempotent")
                return
            connection.execute(
                "UPDATE pair_ledger SET freeze_intent_hash = ?, "
                "freeze_intent_locator = ? WHERE id = 1",
                (prediction_set_hash, canonical_locator),
            )
            self._commit(connection, "prepare_freeze")

    @_guarded_write("pre-label reservation failed or was interrupted")
    def reserve_evaluation(
        self, *, audit_export_hash: str, manual_audit_hash: str
    ) -> PrelabelReservation:
        """Atomically freeze the exact pre-label audit identity."""
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
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
            self._commit(connection, "reserve_prelabel")
            return PrelabelReservation(
                LedgerState.PRELABEL_FROZEN,
                token,
                token,
                expires_at,
            )

    @_guarded_write("label open failed or was interrupted")
    def reserve_label_open(
        self,
        *,
        audit_export_hash: str,
        manual_audit_hash: str,
        reservation_token: str,
    ) -> LabelOpenReservation:
        """Perform the sole atomic label-open transition."""
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
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
            self._commit(connection, "reserve_label_open")
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
        expected_label_manifest_hash: str,
    ) -> None:
        """Read-only child-process fence immediately before labels are read."""
        with self._connection() as connection:
            row = self._require_pair(connection)
            self._require_authorized_lineage(row)
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
        # H1: label 身份只信固定 custodian manifest，调用方参数必须与其一致；
        # 不一致在首次读取/解析 label、构造结果、写 artifact 之前 fail closed。
        if (
            not _valid_hash(expected_label_manifest_hash)
            or expected_label_manifest_hash != self._manifest.label_manifest_hash
        ):
            raise ValueError("label manifest identity differs from custodian manifest")

    @_guarded_write("custodian lease heartbeat failed or was interrupted")
    def heartbeat(
        self, lease_token: str, *, now: float | datetime | None = None
    ) -> float:
        """Extend one live custodian lease; stale holders cannot revive it."""
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
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
            self._commit(connection, "heartbeat")
            return expires_at

    @_guarded_write("label-side evaluation completion failed or was interrupted")
    def mark_evaluation_completed(
        self, evaluation_artifact_hash: str, *, lease_token: str
    ) -> None:
        if not _valid_hash(evaluation_artifact_hash):
            raise ValueError("evaluation artifact hash must be sha256")
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
            if LedgerState(row["state"]) != LedgerState.LABELS_OPEN:
                raise ValueError("pair is not open for evaluation completion")
            self._require_lease(row, token=lease_token, kind="label_open")
            connection.execute(
                "UPDATE pair_ledger SET state = ?, evaluation_artifact_hash = ?, "
                "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
                "lease_expires_at = NULL WHERE id = 1",
                (LedgerState.COMPLETED.value, evaluation_artifact_hash),
            )
            self._commit(connection, "complete_evaluation")

    @_guarded_write("prediction side start failed or was interrupted")
    def record_side_started(self, side: str, output_dir: str) -> str:
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
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
            # 调用方 locator 会作为身份字段持久化；若按当前 cwd 解析相对路径，
            # ledger 就会依赖进程 cwd，并允许同一 side 以另一种 custodian 拼写重放。
            # 输出目录此时尚未创建，因此绑定最近存在祖先的最终句柄拼写。
            output_locator = canonical_creation_locator(Path(output_dir).expanduser())
            output_volume = volume_identity(Path(output_locator))
            token = secrets.token_urlsafe(32)
            expires_at = _now() + float(row["lease_seconds"])
            connection.execute(
                "INSERT INTO pair_sides(side, output_dir, output_locator, output_volume, status) "
                "VALUES (?, ?, ?, ?, 'started')",
                (side, output_locator, output_locator, output_volume),
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
            self._commit(connection, "start_prediction_side")
            return token

    @_guarded_write("prediction side completion failed or was interrupted")
    def record_side_completed(
        self, side: str, bundle_hash: str, *, lease_token: str
    ) -> None:
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=_now())
            self._require_authorized_lineage(row)
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
            self._commit(connection, "complete_prediction_side")

    def invalidate_pair(self, reason: str) -> None:
        try:
            self._invalidate_pair(reason)
        except sqlite3.OperationalError as exc:
            # 观察到失败边界的进程也可能被同一 SQLite writer lock 阻塞；此时先
            # 持久化 custodian recovery intent，交由下一次 custodian 入口收敛状态。
            self._write_failure_intent(reason)
            raise exc

    def _invalidate_pair(self, reason: str) -> None:
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            self._require_pair(connection)
            self._fail_pair(connection, reason)
            self._commit(connection, "invalidate_pair")

    @_guarded_write("custodian recovery failed or was interrupted")
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
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=current)
            self._require_authorized_lineage(row)
            self._commit(connection, "recover_expired")
            return LedgerState(row["state"])

    @_guarded_write("custodian reconcile failed or was interrupted")
    def reconcile_pending_failure(
        self, *, now: float | datetime | None = None
    ) -> LedgerState:
        """Unified custodian startup/restart reconcile.

        Converges the durable failure intent (via the connection boundary) and
        every expired in-flight lease in one idempotent entry, so a restart
        never depends on a future business command to reach a verifiable state.
        """
        current = _coerce_now(now)
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
            row = self._require_pair(connection)
            row = self._recover_if_expired(connection, row, now=current)
            self._require_authorized_lineage(row)
            self._commit(connection, "reconcile_pending_failure")
            return LedgerState(row["state"])

    @_guarded_write("pair reauthorization failed or was interrupted")
    def reauthorize(
        self, owner_token: str, *, authorized_evaluation_identity: str
    ) -> None:
        if not owner_token.strip():
            raise ValueError("explicit non-empty reauthorization is required")
        if not _valid_hash(authorized_evaluation_identity):
            raise ValueError("authorized evaluation identity must be sha256")
        token_hash = _token_hash(owner_token)
        with self._connection() as connection:
            _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
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
            next_identity = _ledger_identity(
                canonical_root=self._manifest.canonical_root,
                manifest_hash=self._manifest.manifest_hash,
                partition=row["partition"],
                prediction_set_hash=authorized_evaluation_identity,
                expected_sides=_decode_sides(row["expected_sides"]),
                lineage_identity_hash=authorized_evaluation_identity,
            )
            connection.execute(
                "UPDATE pair_ledger SET state = ?, prediction_set_hash = ?, "
                "prediction_set_bound = 0, ledger_identity = ?, "
                "prelabel_reservation_hash = NULL, audit_export_hash = NULL, "
                "manual_audit_hash = NULL, label_open_count = 0, "
                "label_lineage_hash = NULL, "
                "freeze_intent_hash = NULL, freeze_intent_locator = NULL, "
                "lineage_identity_hash = ?, authorized_evaluation_identity_hash = ?, "
                "evaluation_artifact_hash = NULL, failure_reason = NULL, "
                "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
                "lease_expires_at = NULL, reauthorization_epoch = reauthorization_epoch + 1 "
                "WHERE id = 1",
                (
                    LedgerState.NEW.value,
                    authorized_evaluation_identity,
                    next_identity,
                    authorized_evaluation_identity,
                    authorized_evaluation_identity,
                ),
            )
            connection.execute("DELETE FROM pair_sides")
            connection.execute(
                "INSERT INTO reauthorizations(epoch, token_hash, evaluation_identity_hash) "
                "VALUES (?, ?, ?)",
                (row["reauthorization_epoch"] + 1, token_hash, authorized_evaluation_identity),
            )
            self._commit(connection, "reauthorize")

    def has_valid_runtime_verification_receipt(
        self,
        pair_root: Path,
        *,
        pair_identity: str,
        runtime_root: Path,
        runtime_manifest_hash: str,
    ) -> bool:
        """检查首侧完整 runtime 校验留下的 custodian seal receipt。"""
        receipt_path, payload = self._runtime_verification_receipt(
            pair_root,
            pair_identity=pair_identity,
            runtime_root=runtime_root,
            runtime_manifest_hash=runtime_manifest_hash,
        )
        if not receipt_path.exists():
            return False
        self._assert_runtime_verification_receipt(receipt_path, payload)
        return True

    def ensure_runtime_verification_receipt(
        self,
        pair_root: Path,
        *,
        pair_identity: str,
        runtime_root: Path,
        runtime_manifest_hash: str,
    ) -> None:
        """原子创建或复核 pair 级 runtime 完整校验凭证。"""
        receipt_path, payload = self._runtime_verification_receipt(
            pair_root,
            pair_identity=pair_identity,
            runtime_root=runtime_root,
            runtime_manifest_hash=runtime_manifest_hash,
        )
        encoded = _canonical_json(
            {
                **payload,
                "mac": self._runtime_verification_mac(payload),
            }
        )
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_path(receipt_path.parent, "runtime verification receipt directory")
        try:
            with receipt_path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(encoded)
        except FileExistsError:
            self._assert_runtime_verification_receipt(receipt_path, payload)

    def _runtime_verification_receipt(
        self,
        pair_root: Path,
        *,
        pair_identity: str,
        runtime_root: Path,
        runtime_manifest_hash: str,
    ) -> tuple[Path, dict[str, str]]:
        if self._manifest is None:
            raise ValueError("custodian manifest is required")
        if not _valid_hash(pair_identity) or not _valid_hash(runtime_manifest_hash):
            raise ValueError("runtime verification identities must be sha256")
        pair_locator = canonical_locator(Path(pair_root))
        pair_path = Path(pair_locator)
        canonical_root = Path(self._manifest.canonical_root)
        if pair_path != canonical_root and canonical_root not in pair_path.parents:
            raise ValueError("runtime verification receipt escapes custodian root")
        runtime_locator = canonical_creation_locator(Path(runtime_root))
        payload = {
            "schema_version": "rcaeval-runtime-verification-v1",
            "pair_identity": pair_identity,
            "runtime_locator": runtime_locator,
            "runtime_manifest_hash": runtime_manifest_hash,
            "custodian_manifest_hash": self._manifest.manifest_hash,
        }
        receipt_path = (
            canonical_root
            / RUNTIME_VERIFICATION_DIRNAME
            / f"{pair_identity}.json"
        )
        return receipt_path, payload

    def _runtime_verification_mac(self, payload: dict[str, str]) -> str:
        if self._seal_key is None:
            raise ValueError("custodian seal key is required")
        return hmac.new(
            self._seal_key,
            _canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _assert_runtime_verification_receipt(
        self, path: Path, expected_payload: dict[str, str]
    ) -> None:
        reject_reparse_path(path, "runtime verification receipt")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("runtime verification receipt is unreadable") from exc
        if not isinstance(payload, dict) or set(payload) != {
            *expected_payload,
            "mac",
        }:
            raise ValueError("runtime verification receipt schema is invalid")
        actual_payload = {key: payload[key] for key in expected_payload}
        if actual_payload != expected_payload:
            raise ValueError("runtime verification receipt identity differs")
        mac = payload["mac"]
        if not isinstance(mac, str) or not _valid_hash(mac):
            raise ValueError("runtime verification receipt MAC is invalid")
        if not hmac.compare_digest(mac, self._runtime_verification_mac(expected_payload)):
            raise ValueError("runtime verification receipt MAC is invalid")

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

    def side_records(self) -> dict[str, dict[str, str | None]]:
        """Return the custodian-frozen output binding for every prediction side."""
        with self._connection() as connection:
            self._require_pair(connection)
            rows = connection.execute(
                "SELECT side, output_dir, output_locator, output_volume, status, bundle_hash "
                "FROM pair_sides ORDER BY side"
            ).fetchall()
            return {
                row["side"]: {
                    "output_dir": row["output_dir"],
                    "output_locator": row["output_locator"],
                    "output_volume": row["output_volume"],
                    "status": row["status"],
                    "bundle_hash": row["bundle_hash"],
                }
                for row in rows
            }

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
        return connection.execute("SELECT * FROM pair_ledger WHERE id = 1").fetchone()

    def _fail_pair(self, connection: sqlite3.Connection, reason: str) -> None:
        connection.execute(
            "UPDATE pair_ledger SET state = ?, failure_reason = ?, "
            "lease_kind = NULL, lease_token_hash = NULL, lease_side = NULL, "
            "lease_expires_at = NULL WHERE id = 1",
            (LedgerState.FAILED_NON_RESUMABLE.value, reason[:256]),
        )
        connection.execute("UPDATE pair_sides SET status = 'invalidated'")

    def _invalidate_after_write_error(
        self, reason: str, original: sqlite3.OperationalError
    ) -> None:
        try:
            self.invalidate_pair(reason)
        except BaseException as cleanup_error:
            # pair row 尚不存在（首次 initialize 失败）时没有可收敛的状态；
            # 写 intent 反而会让后续合法 initialize 永久 fail closed。
            if self._pair_row_exists():
                self._write_failure_intent(reason)
            raise original from cleanup_error

    def _pair_row_exists(self) -> bool:
        try:
            connection = sqlite3.connect(self.path, timeout=0)
            try:
                return (
                    connection.execute(
                        "SELECT 1 FROM pair_ledger WHERE id = 1"
                    ).fetchone()
                    is not None
                )
            finally:
                connection.close()
        except sqlite3.Error:
            # 无法判定时偏向 fail-closed：持久化 intent 交由 custodian 收敛。
            return True

    def _write_failure_intent(self, reason: str) -> None:
        path = self.failure_intent_path
        payload = {
            "schema_version": "rcaeval-pair-failure-intent-v1",
            "ledger_path": _canonical_path(self.path),
            "custodian_manifest_hash": self.custodian_manifest_hash,
            "reason": reason[:256],
        }
        sealed = dict(payload, intent_hash=canonical_json_sha256(payload))
        encoded = _canonical_json(sealed)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("pair failure intent is unreadable") from exc
            if existing != sealed:
                raise ValueError("pair failure intent belongs to a different ledger")
            return
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(encoded, encoding="utf-8", newline="\n")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _reconcile_failure_intent(self, connection: sqlite3.Connection) -> None:
        path = self.failure_intent_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("pair failure intent is unreadable") from exc
        required = {
            "schema_version",
            "ledger_path",
            "custodian_manifest_hash",
            "reason",
            "intent_hash",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("pair failure intent schema is not frozen")
        identity = {key: payload[key] for key in required if key != "intent_hash"}
        if payload["schema_version"] != "rcaeval-pair-failure-intent-v1":
            raise ValueError("pair failure intent schema is not frozen")
        if payload["ledger_path"] != _canonical_path(self.path):
            raise ValueError("pair failure intent ledger identity differs")
        if payload["custodian_manifest_hash"] != self.custodian_manifest_hash:
            raise ValueError("pair failure intent custodian identity differs")
        if canonical_json_sha256(identity) != payload["intent_hash"]:
            raise ValueError("pair failure intent hash is invalid")
        _begin_immediate(connection, self.SQLITE_BUSY_TIMEOUT_SECONDS)
        row = self._require_pair(connection)
        if LedgerState(row["state"]) != LedgerState.FAILED_NON_RESUMABLE:
            self._fail_pair(connection, str(payload["reason"]))
            self._commit(connection, "failure_intent_reconcile")
        else:
            connection.rollback()
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _require_authorized_lineage(row: sqlite3.Row) -> None:
        if row["authorized_evaluation_identity_hash"] != row["prediction_set_hash"]:
            raise ValueError("authorized evaluation identity is stale")

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

    @contextmanager
    def _connection(self):
        if self._manifest is None:
            raise ValueError("custodian manifest is required for pair ledger access")
        connection = sqlite3.connect(
            self.path,
            timeout=0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 0")
        try:
            self._reconcile_failure_intent(connection)
            yield connection
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _commit(self, connection: sqlite3.Connection, event_kind: str) -> None:
        self._append_seal(connection, event_kind)
        connection.commit()

    def _append_seal(self, connection: sqlite3.Connection, event_kind: str) -> None:
        snapshot = _canonical_json(self._sealed_snapshot(connection))
        previous = connection.execute(
            "SELECT event_hash FROM ledger_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous_hash = "" if previous is None else str(previous[0])
        event_hash = _event_hash(event_kind, snapshot, previous_hash)
        cursor = connection.execute(
            "INSERT INTO ledger_events(event_kind, snapshot, prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?)",
            (event_kind, snapshot, previous_hash, event_hash),
        )
        self._append_anchor(int(cursor.lastrowid), event_hash)

    def _append_anchor(self, seq: int, event_hash: str) -> None:
        """Append the HMAC-chained external anchor entry for one sealed event."""
        chain = self._read_anchor_chain()
        if chain:
            last_seq, _, previous_mac = chain[-1]
            if last_seq != seq - 1:
                raise ValueError("pair ledger seal anchor is not contiguous")
        else:
            if seq != 1:
                raise ValueError("pair ledger seal anchor is missing")
            previous_mac = ""
        mac = self._anchor_mac(seq, event_hash, previous_mac)
        line = (
            json.dumps(
                {"seq": seq, "event_hash": event_hash, "mac": mac},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with self.seal_anchor_path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line)

    def _read_anchor_chain(self) -> list[tuple[int, str, str]]:
        path = self.seal_anchor_path
        if not path.is_file():
            return []
        chain: list[tuple[int, str, str]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError("pair ledger seal anchor is unreadable") from exc
        for line in lines:
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise ValueError("pair ledger seal anchor line is invalid") from exc
            if not isinstance(payload, dict) or set(payload) != {"seq", "event_hash", "mac"}:
                raise ValueError("pair ledger seal anchor schema is not frozen")
            seq = payload["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                raise ValueError("pair ledger seal anchor sequence is invalid")
            if not _valid_hash(str(payload["event_hash"])) or not _valid_hash(
                str(payload["mac"])
            ):
                raise ValueError("pair ledger seal anchor hashes are invalid")
            chain.append((seq, str(payload["event_hash"]), str(payload["mac"])))
        return chain

    def _anchor_mac(self, seq: int, event_hash: str, previous_mac: str) -> str:
        if self._seal_key is None:
            raise ValueError("custodian seal key is required")
        payload = {"seq": seq, "event_hash": event_hash, "previous_mac": previous_mac}
        return hmac.new(
            self._seal_key,
            _canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _assert_sealed(self, connection: sqlite3.Connection) -> None:
        events = connection.execute(
            "SELECT seq, event_kind, snapshot, prev_hash, event_hash "
            "FROM ledger_events ORDER BY seq"
        ).fetchall()
        if not events:
            raise ValueError("pair ledger seal history is missing")
        previous_hash = ""
        for event in events:
            if event["prev_hash"] != previous_hash:
                raise ValueError("pair ledger seal history is tampered")
            if (
                _event_hash(event["event_kind"], event["snapshot"], event["prev_hash"])
                != event["event_hash"]
            ):
                raise ValueError("pair ledger seal history hash is invalid")
            try:
                json.loads(event["snapshot"])
            except (TypeError, ValueError) as exc:
                raise ValueError("pair ledger seal snapshot is invalid") from exc
            previous_hash = event["event_hash"]
        current = _canonical_json(self._sealed_snapshot(connection))
        if events[-1]["snapshot"] != current:
            raise ValueError("pair ledger sealed state is tampered")
        # H3: seal 可信性必须锚定在 SQLite 可变状态之外。没有有效外部 anchor
        # （HMAC 链与事件逐条对齐）时拒绝任何形式的 seal 重建。
        chain = self._read_anchor_chain()
        if len(chain) != len(events):
            raise ValueError("pair ledger seal anchor is missing or diverged")
        previous_mac = ""
        for event, (seq, event_hash, mac) in zip(events, chain, strict=True):
            if seq != event["seq"] or event_hash != event["event_hash"]:
                raise ValueError("pair ledger seal anchor diverges from event history")
            expected = self._anchor_mac(seq, event_hash, previous_mac)
            if not hmac.compare_digest(expected, mac):
                raise ValueError("pair ledger seal anchor MAC is invalid")
            previous_mac = mac

    @staticmethod
    def _sealed_snapshot(connection: sqlite3.Connection) -> dict[str, object]:
        pair = connection.execute("SELECT * FROM pair_ledger WHERE id = 1").fetchone()
        if pair is None:
            raise ValueError("pair ledger has not been initialized by the custodian")
        sides = connection.execute(
            "SELECT side, output_dir, output_locator, output_volume, status, bundle_hash "
            "FROM pair_sides ORDER BY side"
        ).fetchall()
        reauthorizations = connection.execute(
            "SELECT epoch, token_hash, evaluation_identity_hash "
            "FROM reauthorizations ORDER BY epoch"
        ).fetchall()
        return {
            "pair": {key: pair[key] for key in pair.keys()},
            "sides": [{key: row[key] for key in row.keys()} for row in sides],
            "reauthorizations": [
                {key: row[key] for key in row.keys()} for row in reauthorizations
            ],
        }

    def _require_pair(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM pair_ledger WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("pair ledger has not been initialized by the custodian")
        self._assert_sealed(connection)
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


def _require_locator_form(path: Path) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute() or any(part in {".", ".."} for part in raw.parts):
        raise ValueError("ledger locator must be absolute and traversal-free")
    if os.path.normpath(str(raw)).startswith("\\\\"):
        raise ValueError("ledger locator rejects UNC paths")
    return raw


def canonical_locator(path: Path) -> str:
    """Bind the final on-disk handle spelling of an existing path.

    拒绝 UNC、drive/大小写/8.3 别名、junction/symlink/reparse 别名与不存在的
    路径；正常拼写的 Windows/POSIX 路径原样通过。
    """
    raw = _require_locator_form(path)
    reject_reparse_path(raw, "ledger locator")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ValueError("ledger locator path does not exist") from exc
    raw_text = os.path.normpath(str(raw))
    resolved_text = os.path.normpath(str(resolved))
    if resolved_text.startswith("\\\\"):
        raise ValueError("ledger locator rejects UNC paths")
    if raw_text != resolved_text:
        raise ValueError("ledger locator is not canonical")
    return resolved_text


def canonical_creation_locator(path: Path) -> str:
    """为尚未创建的输出路径定位：尾部可不存在，最近存在祖先必须严格绑定。"""
    raw = _require_locator_form(path)
    if raw.exists():
        return canonical_locator(raw)
    ancestor = raw
    tail: list[str] = []
    while not ancestor.exists():
        tail.insert(0, ancestor.name)
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise ValueError("ledger locator ancestor is not a directory")
    base = canonical_locator(ancestor)
    return os.path.normpath(os.path.join(base, *tail))


_canonical_locator = canonical_creation_locator


def volume_identity(path: Path) -> str:
    candidate = Path(path)
    try:
        existing = candidate if candidate.exists() else candidate.parent
        stat = existing.stat()
    except OSError as exc:
        raise ValueError("output locator volume cannot be inspected") from exc
    drive = candidate.drive.upper() if os.name == "nt" else ""
    return f"{drive}:{int(stat.st_dev)}"


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _event_hash(event_kind: str, snapshot: str, previous_hash: str) -> str:
    payload = {
        "event_kind": event_kind,
        "snapshot": snapshot,
        "previous_hash": previous_hash,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _load_custodian_manifest(path: Path) -> CustodianRootManifest:
    # H5: 任何 resolve/open 之前，先拒绝 root、所有父目录与固定文件上的
    # symlink/junction/reparse；Windows 真实 junction 与 POSIX symlink 都 fail-closed。
    raw_path = Path(path).expanduser()
    reject_reparse_path(raw_path, "custodian manifest")
    try:
        manifest_path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("custodian manifest is unreadable") from exc
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
    stored_root = str(payload["canonical_root"])
    root_raw = Path(stored_root)
    if not root_raw.is_absolute() or os.path.normpath(stored_root) != stored_root:
        raise ValueError("custodian manifest canonical root is not normalized")
    reject_reparse_path(root_raw, "custodian root")
    try:
        root = os.path.normpath(str(root_raw.resolve(strict=True)))
    except OSError as exc:
        raise ValueError("custodian manifest canonical root is unreadable") from exc
    if root != stored_root:
        raise ValueError("custodian manifest canonical root is not the final spelling")
    if (
        os.path.normpath(str(manifest_path.parent)) != root
        or manifest_path.name != "custodian-manifest.json"
    ):
        raise ValueError("custodian manifest is not at its canonical custodian root")
    # 固定文件的递归 entries 同样不得含 reparse/symlink。边迭代边检查：
    # rglob 会递归进入 junction（junction 非 symlink），先物化的 sorted()
    # 在指向祖先的 junction 环下指数膨胀挂起；条目一产出即拒绝则绝不递归进入。
    for entry in Path(root).rglob("*"):
        reject_reparse_path(entry, "custodian root entry")
    if payload["ledger_filename"] != "pair-ledger.sqlite3":
        raise ValueError("custodian ledger filename is not frozen")
    if not _valid_hash(str(payload["runtime_manifest_hash"])) or not _valid_hash(
        str(payload["label_manifest_hash"])
    ):
        raise ValueError("custodian manifest identities must be sha256")
    identity = {key: payload[key] for key in required if key != "manifest_hash"}
    if canonical_json_sha256(identity) != payload["manifest_hash"]:
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
    lineage_identity_hash: str,
) -> str:
    payload = {
        "canonical_root": canonical_root,
        "custodian_manifest_hash": manifest_hash,
        "partition": partition,
        "prediction_set_hash": prediction_set_hash,
        "expected_sides": sorted(expected_sides),
        "lineage_identity_hash": lineage_identity_hash,
    }
    return canonical_json_sha256(payload)


def _lineage_hash(audit_export_hash: str, manual_audit_hash: str) -> str:
    return canonical_json_sha256(
        {
            "audit_export_hash": audit_export_hash,
            "manual_audit_hash": manual_audit_hash,
        }
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _begin_immediate(connection: sqlite3.Connection, timeout_seconds: float) -> None:
    """Acquire the pair write lock with one bounded, shared backoff policy."""
    deadline = time.monotonic() + timeout_seconds
    delay = 0.01
    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.25)


def _now() -> float:
    return time.time()


def _coerce_now(value: float | datetime | None) -> float:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)
