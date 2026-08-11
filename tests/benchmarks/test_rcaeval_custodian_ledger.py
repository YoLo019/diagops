from __future__ import annotations

import sqlite3
import time

import pytest

from backend.benchmarks.rcaeval.ledger import (
    CustodianPairLedger,
    LedgerState,
)


def test_label_open_is_bound_to_one_pair_across_output_directories(tmp_path):
    ledger = CustodianPairLedger(tmp_path / "custodian.sqlite3")
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )
    single_lease = ledger.record_side_started("single_intended", "prediction-a")
    ledger.record_side_completed("single_intended", "d" * 64, lease_token=single_lease)
    multi_lease = ledger.record_side_started("multi_intended", "prediction-b")
    ledger.record_side_completed("multi_intended", "e" * 64, lease_token=multi_lease)
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
    with pytest.raises(ValueError, match="lease token"):
        ledger.assert_label_open(
            partition="tt90",
            prediction_set_hash="a" * 64,
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
        )

    with pytest.raises(ValueError, match="label open|non-resumable|already"):
        CustodianPairLedger(tmp_path / "custodian.sqlite3").reserve_label_open(
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            reservation_token=reservation.reservation_token,
        )


def test_label_open_requires_frozen_audit_identity_and_fail_closed_after_crash(tmp_path):
    ledger = CustodianPairLedger(tmp_path / "custodian.sqlite3")
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(side, f"prediction-{side}")
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
    ledger.bind_prediction_set_hash("a" * 64)
    reservation = ledger.reserve_evaluation(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
    )

    with pytest.raises(ValueError, match="pre-label|audit|reauthor"):
        CustodianPairLedger(tmp_path / "custodian.sqlite3").reserve_label_open(
            audit_export_hash="d" * 64,
            manual_audit_hash="c" * 64,
            reservation_token=reservation.reservation_token,
        )

    with pytest.raises(ValueError, match="reservation"):
        CustodianPairLedger(tmp_path / "custodian.sqlite3").reserve_label_open(
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            reservation_token="lost-after-crash",
        )
    assert ledger.snapshot()["state"] == LedgerState.PRELABEL_FROZEN.value

    CustodianPairLedger(tmp_path / "custodian.sqlite3").reserve_label_open(
        audit_export_hash="b" * 64,
        manual_audit_hash="c" * 64,
        reservation_token=reservation.reservation_token,
    )
    with pytest.raises(ValueError, match="already|non-resumable"):
        CustodianPairLedger(tmp_path / "custodian.sqlite3").reserve_label_open(
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
            reservation_token=reservation.reservation_token,
        )


def test_failed_pair_needs_explicit_reauthorization_for_both_sides(tmp_path):
    ledger = CustodianPairLedger(tmp_path / "custodian.sqlite3")
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )
    ledger.record_side_started("single_intended", "output-a")
    ledger.invalidate_pair("transport failure")

    with pytest.raises(ValueError, match="reauthor"):
        CustodianPairLedger(tmp_path / "custodian.sqlite3").record_side_started(
            "multi_intended", "output-b"
        )
    CustodianPairLedger(tmp_path / "custodian.sqlite3").reauthorize("owner-token")
    ledger2 = CustodianPairLedger(tmp_path / "custodian.sqlite3")
    ledger2.record_side_started("single_intended", "output-c")
    with pytest.raises(ValueError, match="both|pair"):
        ledger2.record_side_started("single_intended", "output-d")


def test_inflight_prediction_crash_blocks_the_other_side(tmp_path):
    ledger = CustodianPairLedger(tmp_path / "custodian.sqlite3")
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
    )
    ledger.record_side_started("single_intended", "output-a")

    with pytest.raises(ValueError, match="lease token"):
        ledger.record_side_completed("single_intended", "d" * 64)

    with pytest.raises(ValueError, match="in-flight|reauthorization"):
        ledger.record_side_started("multi_intended", "output-b")


def _seed_pair_for_recovery(tmp_path, *, lease_seconds=1):
    ledger = CustodianPairLedger(tmp_path / "recovery.sqlite3")
    ledger.initialize(
        partition="tt90",
        prediction_set_hash="a" * 64,
        expected_sides=("single_intended", "multi_intended"),
        lease_seconds=lease_seconds,
    )
    return ledger


def test_prediction_lease_expiry_recovers_both_sides_after_completion_lock(tmp_path):
    ledger = _seed_pair_for_recovery(tmp_path)
    lease = ledger.record_side_started("single_intended", "output-a")

    blocker = sqlite3.connect(ledger.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.record_side_completed("single_intended", "d" * 64, lease_token=lease)
    finally:
        blocker.rollback()
        blocker.close()

    recovered = CustodianPairLedger(ledger.path).recover_expired(now=time.time() + 10)
    assert recovered == LedgerState.FAILED_NON_RESUMABLE
    snapshot = ledger.snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert ledger.side_snapshot() == {"single_intended": "invalidated"}
    ledger.reauthorize("authorized-retry")
    assert ledger.snapshot()["state"] == LedgerState.NEW.value


def test_label_open_lease_expiry_requires_explicit_reauthorization(tmp_path):
    ledger = _seed_pair_for_recovery(tmp_path)
    for side, bundle_hash in (
        ("single_intended", "d" * 64),
        ("multi_intended", "e" * 64),
    ):
        lease = ledger.record_side_started(side, f"output-{side}")
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
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
    with pytest.raises(ValueError, match="reauthor"):
        ledger.assert_label_open(
            partition="tt90",
            prediction_set_hash="a" * 64,
            audit_export_hash="b" * 64,
            manual_audit_hash="c" * 64,
        )
    ledger.reauthorize("authorized-label-retry")
    assert ledger.snapshot()["label_open_count"] == 0
