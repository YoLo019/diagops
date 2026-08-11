from __future__ import annotations

import inspect
import sqlite3
import time

import pytest

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
        lease = ledger.record_side_started(side, f"prediction-{side}")
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
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
    ledger.record_side_started("single_intended", "output")
    ledger.invalidate_pair("second failure")
    with pytest.raises(ValueError, match="already consumed|failed pair"):
        _reauthorize(ledger, "owner-once")


def test_label_open_is_bound_to_one_pair_across_output_directories(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("reset fixture")
    _reauthorize(ledger, "fixture-retry")
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
        lease = ledger.record_side_started(side, f"prediction-{side}")
        ledger.record_side_completed(side, bundle_hash, lease_token=lease)
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
    ledger.record_side_started("single_intended", "output-a")
    ledger.invalidate_pair("transport failure")

    with pytest.raises(ValueError, match="reauthor"):
        ledger.record_side_started(
            "multi_intended", "output-b"
        )
    _reauthorize(ledger, "owner-token")
    ledger2 = ledger
    ledger2.record_side_started("single_intended", "output-c")
    with pytest.raises(ValueError, match="both|pair"):
        ledger2.record_side_started("single_intended", "output-d")


def test_inflight_prediction_crash_blocks_the_other_side(tmp_path):
    ledger = _new_ledger(tmp_path)
    ledger.invalidate_pair("reset fixture")
    _reauthorize(ledger, "fixture-retry")
    ledger.record_side_started("single_intended", "output-a")

    with pytest.raises(TypeError):
        ledger.record_side_completed("single_intended", "d" * 64)

    with pytest.raises(ValueError, match="in-flight|reauthorization"):
        ledger.record_side_started("multi_intended", "output-b")


def _seed_pair_for_recovery(tmp_path, *, lease_seconds=1):
    return _new_ledger(tmp_path, lease_seconds=lease_seconds)


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

    recovered = CustodianPairLedger.from_manifest(
        tmp_path / "custodian-root" / "custodian-manifest.json"
    ).recover_expired(now=time.time() + 10)
    assert recovered == LedgerState.FAILED_NON_RESUMABLE
    snapshot = ledger.snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
    assert ledger.side_snapshot() == {"single_intended": "invalidated"}
    _reauthorize(ledger, "authorized-retry")
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
