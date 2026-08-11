from __future__ import annotations

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
    ledger.record_side_started("single_intended", "prediction-a")
    ledger.record_side_completed("single_intended", "d" * 64)
    ledger.record_side_started("multi_intended", "prediction-b")
    ledger.record_side_completed("multi_intended", "e" * 64)
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
        ledger.record_side_started(side, f"prediction-{side}")
        ledger.record_side_completed(side, bundle_hash)
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

    with pytest.raises(ValueError, match="in-flight|reauthorization"):
        ledger.record_side_started("multi_intended", "output-b")
