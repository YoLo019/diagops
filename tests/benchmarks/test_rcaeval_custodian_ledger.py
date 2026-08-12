from __future__ import annotations

import inspect
import os
import sqlite3
import subprocess
import threading
import time

import pytest

import backend.benchmarks.rcaeval.ledger as ledger_module
from backend.benchmarks.rcaeval.ledger import (
    CustodianPairLedger,
    LedgerState,
    create_custodian_manifest,
)


def _new_ledger(tmp_path, *, lease_seconds=900):
    manifest = create_custodian_manifest(
        tmp_path / "custodian-root",
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    ledger = CustodianPairLedger.from_manifest(manifest)
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
        lease_seconds=lease_seconds,
    )
    return ledger


def _freeze_and_open(ledger):
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(
            side, str(ledger.canonical_root / f"prediction-{side}")
        )
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="a" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("a" * 64)
    reservation = ledger.reserve_evaluation(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
    )
    return ledger.reserve_label_open(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
        reservation_token=reservation.reservation_token,
    )


def _reauthorize(ledger, token):
    ledger.reauthorize(token, authorized_evaluation_identity="f" * 64)


def test_custodian_manifest_cannot_be_rebound_to_another_root(tmp_path):
    canonical_root = tmp_path / "custodian"
    manifest = create_custodian_manifest(
        canonical_root,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    ledger = CustodianPairLedger.from_manifest(manifest)
    ledger.initialize(
        partition="ss30",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )

    copied_root = tmp_path / "attacker-root"
    copied_root.mkdir()
    copied_manifest = copied_root / manifest.name
    copied_manifest.write_bytes(manifest.read_bytes())
    with pytest.raises(ValueError, match="canonical custodian root|manifest"):
        CustodianPairLedger.from_manifest(copied_manifest)

    with pytest.raises(ValueError, match="custodian manifest"):
        CustodianPairLedger(copied_root / "pair-ledger.sqlite3").initialize(
            partition="ss30",
            prediction_set_hash="a" * 64,
            expected_sides=("single_intended", "multi_intended"),
        )


def test_prediction_set_bind_transitions_ledger_identity_for_evaluate_reopen(tmp_path):
    ledger = _new_ledger(tmp_path)
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(
            side, str(ledger.canonical_root / f"prediction-{side}")
        )
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)

    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="b" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("b" * 64)
    reopened = CustodianPairLedger.from_manifest(
        tmp_path / "custodian-root" / "custodian-manifest.json"
    )

    # The real evaluate path reopens/initializes the custodian ledger using the
    # final frozen prediction-set identity before reserving evaluation.
    reopened.initialize(
        partition="tt90",
        prediction_set_hash="b" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )
    assert reopened.snapshot()["prediction_set_hash"] == "b" * 64


def test_prediction_freeze_intent_reconciles_after_materialization_before_bind(tmp_path):
    ledger = _new_ledger(tmp_path)
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(side, str(ledger.canonical_root / side))
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
    root = ledger.canonical_root / "predictions"
    root.mkdir()
    digest = "b" * 64
    ledger.prepare_prediction_set_freeze(prediction_set_hash=digest, root_locator=str(root))
    (root / "SHA256SUMS").write_text("materialized\n", encoding="utf-8")

    reopened = CustodianPairLedger.from_manifest(
        tmp_path / "custodian-root" / "custodian-manifest.json"
    )
    reopened.prepare_prediction_set_freeze(prediction_set_hash=digest, root_locator=str(root))
    reopened.bind_prediction_set_hash(digest)
    assert reopened.snapshot()["prediction_set_bound"] == 1


def test_record_side_started_rejects_relative_output_locator(tmp_path):
    ledger = _new_ledger(tmp_path)
    with pytest.raises(ValueError, match="absolute|canonical|locator"):
        ledger.record_side_started("single_intended", "relative-output")
    assert ledger.side_snapshot() == {}


def test_prediction_set_identity_transition_rejects_aba_and_tamper(tmp_path):
    ledger = _new_ledger(tmp_path)
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(
            side, str(ledger.canonical_root / f"prediction-{side}")
        )
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)

    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="b" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("b" * 64)
    with pytest.raises(ValueError, match="differs|identity|stale"):
        ledger.bind_prediction_set_hash("c" * 64)

    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE pair_ledger SET ledger_identity = ? WHERE id = 1",
            ("0" * 64,),
        )
    with pytest.raises(ValueError, match="identity|tampered|differs"):
        CustodianPairLedger.from_manifest(
            tmp_path / "custodian-root" / "custodian-manifest.json"
        ).initialize(
            partition="tt90",
            prediction_set_hash="b" * 64,
            expected_sides=("single_intended", "multi_intended"),
        )


def test_reauthorization_identity_rejects_old_prediction_identity(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("lineage reset")
    ledger.reauthorize(
        "lineage-owner",
        authorized_evaluation_identity="f" * 64,
    )

    with pytest.raises(ValueError, match="authorized|identity|lineage"):
        ledger.initialize(
            partition="tt90",
            prediction_set_hash="a" * 64,
            expected_sides=("single_intended", "multi_intended"),
        )


def test_ledger_connection_is_closed_after_context(tmp_path):
    ledger = _new_ledger(tmp_path)
    with ledger._connection() as connection:
        connection.execute("SELECT 1")

    # sqlite3.Connection.__exit__ commits/rolls back but does not close. The
    # ledger boundary must close the handle so Windows can remove the root.
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_side_completion_waits_for_a_transient_sqlite_writer_lock(tmp_path):
    ledger = _new_ledger(tmp_path)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.1)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        ledger.record_side_completed(
            "single_intended",
            "d" * 64,
            lease_token=lease,
        )
    finally:
        releaser.join(timeout=2)

    assert ledger.snapshot()["state"] == LedgerState.PREDICTING.value


def test_evaluation_completion_waits_for_a_transient_sqlite_writer_lock(tmp_path):
    ledger = _new_ledger(tmp_path)
    opened = _freeze_and_open(ledger)
    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.1)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        ledger.mark_evaluation_completed("a" * 64, lease_token=opened.lease_token)
    finally:
        releaser.join(timeout=2)

    assert ledger.snapshot()["state"] == LedgerState.COMPLETED.value


def test_evaluation_completion_lock_failure_invalidates_label_lineage(
    tmp_path,
):
    ledger = _new_ledger(tmp_path, lease_seconds=30)
    opened = _freeze_and_open(ledger)
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.08)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.mark_evaluation_completed(
                "a" * 64,
                lease_token=opened.lease_token,
            )
    finally:
        releaser.join(timeout=2)

    snapshot = ledger.snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert snapshot["label_open_count"] == 1
    assert snapshot["label_ever_opened"] == 1


def test_long_writer_lock_has_durable_recovery_for_prediction_completion(tmp_path):
    ledger = _new_ledger(tmp_path, lease_seconds=30)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.30)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.record_side_completed("single_intended", "d" * 64, lease_token=lease)
    finally:
        releaser.join(timeout=2)

    # A deterministic custodian read/recovery entry must converge the durable
    # failure intent without another prediction business operation.
    assert ledger.snapshot()["state"] == LedgerState.FAILED_NON_RESUMABLE.value


def test_direct_invalidation_lock_failure_persists_custodian_intent(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(ledger.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            ledger.invalidate_pair("freeze boundary failed")
        assert ledger.failure_intent_path.is_file()
    finally:
        blocker.rollback()
        blocker.close()
    assert ledger.snapshot()["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert not ledger.failure_intent_path.exists()


def test_long_writer_lock_has_durable_recovery_for_evaluation_completion(tmp_path):
    ledger = _new_ledger(tmp_path, lease_seconds=30)
    opened = _freeze_and_open(ledger)
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.30)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.mark_evaluation_completed("a" * 64, lease_token=opened.lease_token)
    finally:
        releaser.join(timeout=2)

    snapshot = ledger.snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert snapshot["label_ever_opened"] == 1


def test_heartbeat_rejects_tampered_authorized_lineage(tmp_path):
    ledger = _new_ledger(tmp_path)
    token = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE pair_ledger SET authorized_evaluation_identity_hash = ? WHERE id = 1",
            ("f" * 64,),
        )
    with pytest.raises(ValueError, match="tamper|seal|identity|authorized"):
        ledger.heartbeat(token)


def test_reauthorization_rejects_cleared_reveal_history(tmp_path):
    ledger = _new_ledger(tmp_path)
    _freeze_and_open(ledger)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE pair_ledger SET state = ?, label_ever_opened = 0 WHERE id = 1",
            (LedgerState.FAILED_NON_RESUMABLE.value,),
        )
    with pytest.raises(ValueError, match="tamper|seal|revealed|reauthor"):
        ledger.reauthorize("replay-owner", authorized_evaluation_identity="f" * 64)


def test_label_reveal_is_consumed_forever_across_reauthorization(tmp_path):
    ledger = _new_ledger(tmp_path)
    _freeze_and_open(ledger)
    assert ledger.snapshot()["label_open_count"] == 1
    ledger.recover_expired(now=time.time() + 10_000)
    with pytest.raises(ValueError, match="reauthor"):
        _reauthorize(ledger, "retry-after-reveal")

    assert ledger.snapshot()["label_ever_opened"] == 1
    assert ledger.snapshot()["reveal_epoch"] == 1


def test_formal_lease_apis_require_a_lease_token_argument():
    for name in ("record_side_completed", "assert_label_open", "mark_evaluation_completed"):
        parameter = inspect.signature(getattr(CustodianPairLedger, name)).parameters[
            "lease_token"
        ]
        assert parameter.default is inspect.Parameter.empty


def test_reauthorization_token_is_one_time_and_reveal_epoch_monotonic(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("first failure")
    _reauthorize(ledger, "owner-once")
    with pytest.raises(ValueError, match="already consumed|failed pair"):
        _reauthorize(ledger, "owner-once")
    ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output")
    )
    ledger.invalidate_pair("second failure")
    with pytest.raises(ValueError, match="already consumed|failed pair"):
        _reauthorize(ledger, "owner-once")


def test_label_open_is_bound_to_one_pair_across_output_directories(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("reset fixture")
    _reauthorize(ledger, "fixture-retry")
    single_lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "prediction-a")
    )
    ledger.record_side_completed("single_intended", "d" * 64, lease_token=single_lease)
    multi_lease = ledger.record_side_started(
        "multi_intended", str(ledger.canonical_root / "prediction-b")
    )
    ledger.record_side_completed("multi_intended", "e" * 64, lease_token=multi_lease)
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="a" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("a" * 64)
    reservation = ledger.reserve_evaluation(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
    )

    opened = ledger.reserve_label_open(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
        reservation_token=reservation.reservation_token,
    )
    assert opened.state == LedgerState.LABELS_OPEN
    assert opened.label_open_count == 1
    with pytest.raises(TypeError):
        ledger.assert_label_open(
            partition="tt90",
            prediction_set_hash="a" * 64,
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
        )

    with pytest.raises(ValueError, match="label open|non-resumable|already"):
        ledger.reserve_label_open(
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            reservation_token=reservation.reservation_token,
        )


def test_label_open_requires_frozen_audit_identity_and_fail_closed_after_crash(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("reset fixture")
    _reauthorize(ledger, "fixture-retry")
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(
            side, str(ledger.canonical_root / f"prediction-{side}")
        )
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="a" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("a" * 64)
    reservation = ledger.reserve_evaluation(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
    )

    with pytest.raises(ValueError, match="pre-label|audit|reauthor"):
        ledger.reserve_label_open(
            audit_export_hash="d" * 64,
            manual_audit_hash="c" * 64,
            reservation_token=reservation.reservation_token,
        )

    with pytest.raises(ValueError, match="reservation"):
        ledger.reserve_label_open(
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            reservation_token="lost-after-crash",
        )
    assert ledger.snapshot()["state"] == LedgerState.PRELABEL_FROZEN.value

    ledger.reserve_label_open(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
        reservation_token=reservation.reservation_token,
    )
    with pytest.raises(ValueError, match="already|non-resumable"):
        ledger.reserve_label_open(
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            reservation_token=reservation.reservation_token,
        )


def test_failed_pair_needs_explicit_reauthorization_for_both_sides(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("reset fixture")
    _reauthorize(ledger, "fixture-retry")
    ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    ledger.invalidate_pair("transport failure")

    with pytest.raises(ValueError, match="reauthor"):
        ledger.record_side_started(
            "multi_intended", str(ledger.canonical_root / "output-b")
        )
    _reauthorize(ledger, "owner-token")
    ledger2 = ledger
    ledger2.record_side_started(
        "single_intended", str(ledger2.canonical_root / "output-c")
    )
    with pytest.raises(ValueError, match="both|pair"):
        ledger2.record_side_started(
            "single_intended", str(ledger2.canonical_root / "output-d")
        )


def test_inflight_prediction_crash_blocks_the_other_side(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("reset fixture")
    _reauthorize(ledger, "fixture-retry")
    ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )

    with pytest.raises(TypeError):
        ledger.record_side_completed("single_intended", "d" * 64)

    with pytest.raises(ValueError, match="in-flight|reauthorization"):
        ledger.record_side_started(
            "multi_intended", str(ledger.canonical_root / "output-b")
        )


def _seed_pair_for_recovery(tmp_path, *, lease_seconds=1):
    return _new_ledger(tmp_path, lease_seconds=lease_seconds)


def test_prediction_lease_expiry_recovers_both_sides_after_completion_lock(tmp_path):
    ledger = _seed_pair_for_recovery(tmp_path)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )

    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.record_side_completed("single_intended", "d" * 64, lease_token=lease)
    finally:
        blocker.rollback()
        blocker.close()

    recovered = CustodianPairLedger.from_manifest(
        tmp_path / "custodian-root" / "custodian-manifest.json"
    ).recover_expired(now=time.time() + 10)
    assert recovered == LedgerState.FAILED_NON_RESUMABLE
    snapshot = ledger.snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert ledger.side_snapshot() == {"single_intended": "invalidated"}
    _reauthorize(ledger, "authorized-retry")
    assert ledger.snapshot()["state"] == LedgerState.NEW.value


def test_prediction_completion_lock_failure_invalidates_without_future_recovery(
    tmp_path,
):
    ledger = _seed_pair_for_recovery(tmp_path, lease_seconds=30)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(
        ledger.path,
        timeout=0,
        isolation_level=None,
        check_same_thread=False,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.08)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.record_side_completed(
                "single_intended",
                "d" * 64,
                lease_token=lease,
            )
    finally:
        releaser.join(timeout=2)

    assert ledger.snapshot()["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert ledger.side_snapshot() == {"single_intended": "invalidated"}


def test_label_open_lease_expiry_requires_explicit_reauthorization(tmp_path):
    ledger = _seed_pair_for_recovery(tmp_path)
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(
            side, str(ledger.canonical_root / f"output-{side}")
        )
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="a" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("a" * 64)
    reservation = ledger.reserve_evaluation(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
    )
    opened = ledger.reserve_label_open(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
        reservation_token=reservation.reservation_token,
    )
    assert opened.lease_token
    assert ledger.recover_expired(now=time.time() + 10) == LedgerState.FAILED_NON_RESUMABLE
    with pytest.raises(TypeError):
        ledger.assert_label_open(
            partition="tt90",
            prediction_set_hash="a" * 64,
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
        )
    with pytest.raises(ValueError, match="revealed|reauthorization"):
        _reauthorize(ledger, "authorized-label-retry")
    assert ledger.snapshot()["label_open_count"] == 1
    assert ledger.snapshot()["label_ever_opened"] == 1


def _forge_appended_seal(ledger, *statements):
    """攻击者用 raw SQLite 改写状态并追加一条公开算法完全合法的 seal event。"""
    with sqlite3.connect(ledger.path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DROP TRIGGER IF EXISTS ledger_events_no_update")
        connection.execute("DROP TRIGGER IF EXISTS ledger_events_no_delete")
        for sql, parameters in statements:
            connection.execute(sql, parameters)
        snapshot = ledger_module._canonical_json(
            ledger_module.CustodianPairLedger._sealed_snapshot(connection)
        )
        previous = connection.execute(
            "SELECT event_hash FROM ledger_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO ledger_events(event_kind, snapshot, prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?)",
            (
                "forged-reveal-reset",
                snapshot,
                previous,
                ledger_module._event_hash("forged-reveal-reset", snapshot, previous),
            ),
        )


def test_forged_seal_cannot_revive_consumed_label_reveal(tmp_path):
    """清零 reveal 后追加伪造 seal：reauthorize 必须 fail closed。"""
    ledger = _new_ledger(tmp_path)
    _freeze_and_open(ledger)
    assert ledger.snapshot()["label_ever_opened"] == 1

    _forge_appended_seal(
        ledger,
        (
            "UPDATE pair_ledger SET state = ?, label_ever_opened = 0, "
            "label_open_count = 0 WHERE id = 1",
            (LedgerState.FAILED_NON_RESUMABLE.value,),
        ),
    )

    with pytest.raises(ValueError, match="anchor|seal|tamper|revealed"):
        ledger.reauthorize("attacker", authorized_evaluation_identity="f" * 64)
    # 篡改后所有读边界同样 fail closed；历史揭盲不得复活
    with pytest.raises(ValueError, match="anchor|seal|tamper"):
        ledger.snapshot()


def test_forged_seal_reopens_no_write_boundary(tmp_path):
    """伪造 seal 后 heartbeat/reserve/complete/assert/recover 全部 fail closed。"""
    ledger = _new_ledger(tmp_path)
    opened = _freeze_and_open(ledger)
    _forge_appended_seal(
        ledger,
        ("UPDATE pair_ledger SET label_ever_opened = 0 WHERE id = 1", ()),
    )

    with pytest.raises(ValueError, match="anchor|seal|tamper"):
        ledger.heartbeat(opened.lease_token)
    with pytest.raises(ValueError, match="anchor|seal|tamper"):
        ledger.assert_label_open(
            partition="tt90",
            prediction_set_hash="a" * 64,
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            lease_token=opened.lease_token,
            expected_label_manifest_hash="2" * 64,
        )
    with pytest.raises(ValueError, match="anchor|seal|tamper"):
        ledger.mark_evaluation_completed("9" * 64, lease_token=opened.lease_token)
    with pytest.raises(ValueError, match="anchor|seal|tamper"):
        ledger.recover_expired(now=time.time() + 10)

    # 伪造回 PREDICTIONS_FROZEN 后 reserve 也不得通过
    _forge_appended_seal(
        ledger,
        (
            "UPDATE pair_ledger SET state = ?, label_ever_opened = 0, "
            "label_open_count = 0, lease_kind = NULL, lease_token_hash = NULL, "
            "lease_expires_at = NULL WHERE id = 1",
            (LedgerState.PREDICTIONS_FROZEN.value,),
        ),
    )
    with pytest.raises(ValueError, match="anchor|seal|tamper"):
        ledger.reserve_evaluation(audit_export_hash="b" * 64, manual_audit_hash="c" * 64)


def test_missing_seal_anchor_refuses_seal_rebuild(tmp_path):
    """外部 anchor 被删除时不得仅凭 SQLite 状态重建 seal。"""
    ledger = _new_ledger(tmp_path)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    anchor = ledger.seal_anchor_path
    assert anchor.is_file()
    anchor.unlink()
    with pytest.raises(ValueError, match="anchor"):
        ledger.record_side_completed("single_intended", "d" * 64, lease_token=lease)
    with pytest.raises(ValueError, match="anchor"):
        ledger.heartbeat(lease)


def test_forged_anchor_line_without_key_fails_closed(tmp_path):
    """攻击者在 anchor 尾部追加无 MAC 行也必须被拒。"""
    ledger = _new_ledger(tmp_path)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    with ledger.seal_anchor_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"event_hash":"' + "0" * 64 + '","mac":"' + "0" * 64 + '","seq":2}\n')
    with pytest.raises(ValueError, match="anchor"):
        ledger.heartbeat(lease)


def test_anchor_key_is_required_for_ledger_access(tmp_path):
    manifest = create_custodian_manifest(
        tmp_path / "custodian-root",
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    ledger = CustodianPairLedger.from_manifest(manifest)
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )
    ledger.seal_key_path.unlink()
    with pytest.raises(ValueError, match="seal key|anchor"):
        CustodianPairLedger.from_manifest(manifest)


def test_reconcile_pending_failure_converges_expired_label_lease(tmp_path):
    """重启统一 reconcile 必须幂等收敛过期 in-flight 状态，不依赖未来业务命令。"""
    ledger = _new_ledger(tmp_path, lease_seconds=1)
    _freeze_and_open(ledger)
    assert (
        ledger.reconcile_pending_failure(now=time.time() + 10)
        == LedgerState.FAILED_NON_RESUMABLE
    )
    assert ledger.snapshot()["label_ever_opened"] == 1
    with pytest.raises(ValueError, match="revealed|reauthor"):
        ledger.reauthorize("owner", authorized_evaluation_identity="f" * 64)


def test_heartbeat_lock_exhaustion_persists_failure_intent(tmp_path):
    ledger = _new_ledger(tmp_path, lease_seconds=30)
    lease = ledger.record_side_started(
        "single_intended", str(ledger.canonical_root / "output-a")
    )
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(ledger.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.heartbeat(lease)
        assert ledger.failure_intent_path.is_file()
    finally:
        blocker.rollback()
        blocker.close()
    assert ledger.snapshot()["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert not ledger.failure_intent_path.exists()


def test_label_open_lock_exhaustion_consumes_pair_via_intent(tmp_path):
    ledger = _new_ledger(tmp_path, lease_seconds=30)
    for side, bundle_hash in (("single_intended", "d" * 64), ("multi_intended", "e" * 64)):
        lease = ledger.record_side_started(side, str(ledger.canonical_root / side))
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="a" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("a" * 64)
    reservation = ledger.reserve_evaluation(
        audit_export_hash="b" * 64, manual_audit_hash="c" * 64
    )
    ledger.SQLITE_BUSY_TIMEOUT_SECONDS = 0.05
    blocker = sqlite3.connect(ledger.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.reserve_label_open(
                audit_export_hash="b" * 64,
                manual_audit_hash="c" * 64,
                reservation_token=reservation.reservation_token,
            )
        assert ledger.failure_intent_path.is_file()
    finally:
        blocker.rollback()
        blocker.close()
    snapshot = ledger.snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    # label 未揭盲时显式 reauthorization 仍可恢复 pair
    ledger.reauthorize("owner-after-lock", authorized_evaluation_identity="f" * 64)
    assert ledger.snapshot()["state"] == LedgerState.NEW.value


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_custodian_manifest_loader_rejects_junction_root(tmp_path):
    real = tmp_path / "real-root"
    manifest = create_custodian_manifest(
        real,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    junction = tmp_path / "junction-root"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="reparse|junction|symlink|canonical"):
        CustodianPairLedger.from_manifest(junction / manifest.name)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_custodian_manifest_loader_rejects_symlink_root(tmp_path):
    real = tmp_path / "real-root"
    manifest = create_custodian_manifest(
        real,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    link = tmp_path / "symlink-root"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="reparse|junction|symlink|canonical"):
        CustodianPairLedger.from_manifest(link / manifest.name)


def test_assert_label_open_binds_custodian_label_manifest_hash(tmp_path):
    """H1: child fence 只信固定 custodian manifest，不信调用方传入的 label 身份。"""
    ledger = _new_ledger(tmp_path)
    opened = _freeze_and_open(ledger)
    assert ledger.label_manifest_hash == "2" * 64

    # 合法但属于另一份 labels 的 hash 必须被拒，且原 CLI 参数路径不得绕过
    with pytest.raises(ValueError, match="label manifest"):
        ledger.assert_label_open(
            partition="tt90",
            prediction_set_hash="a" * 64,
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            lease_token=opened.lease_token,
            expected_label_manifest_hash="d" * 64,
        )
    ledger.assert_label_open(
        partition="tt90",
        prediction_set_hash="a" * 64,
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
        lease_token=opened.lease_token,
        expected_label_manifest_hash="2" * 64,
    )
