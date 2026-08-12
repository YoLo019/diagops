from datetime import UTC, datetime

import pytest

from backend.benchmarks.rcaeval.audit import (
    export_evidence_pairs,
    freeze_manual_audit,
    validate_prelabel_audit,
)
from backend.benchmarks.rcaeval.models import (
    CandidatePrediction,
    CasePrediction,
    EvidenceAuditDecision,
    PredictionBundle,
    RcaEvalConfiguration,
)


def _prediction() -> CasePrediction:
    return CasePrediction(
        case_id="re2-aaaaaaaaaaaaaaaa",
        configuration=RcaEvalConfiguration.MULTI_INTENDED,
        completed=True,
        candidates=[
            CandidatePrediction(
                affected_service="checkout",
                failure_mechanism="cpu stress",
                evidence_ids=["evidence-1", "evidence-2"],
            )
        ],
        runtime_run_id="run-1",
        execution_contract_hash="a" * 64,
        available_evidence_ids=["evidence-1", "evidence-2"],
        evidence_summaries={"evidence-1": "cpu high", "evidence-2": "latency rose"},
    )


def test_audit_export_and_freeze_require_complete_unique_four_part_rubric():
    export = export_evidence_pairs([_prediction()], labels_visible=False)
    decisions = [
        EvidenceAuditDecision(
            pair_id=pair.pair_id,
            entity_supported=True,
            temporally_compatible=True,
            mechanism_relevant=True,
            not_contradicted=True,
            reason_code="supported",
            reviewer_id="reviewer-project-owner",
            reviewed_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        for pair in export.pairs
    ]

    artifact = freeze_manual_audit(export, decisions)

    assert len(export.pairs) == 2
    assert artifact.pass_rate == 1
    assert len(artifact.artifact_hash) == 64
    with pytest.raises(ValueError, match="complete"):
        freeze_manual_audit(export, decisions[:1])
    with pytest.raises(ValueError, match="duplicate"):
        freeze_manual_audit(export, decisions + decisions[:1])


def test_audit_export_fails_if_labels_are_visible():
    with pytest.raises(ValueError, match="labels"):
        export_evidence_pairs([_prediction()], labels_visible=True)


def test_audit_rejects_duplicate_pairs_and_empty_support_denominator():
    prediction = _prediction()
    duplicate_reference = prediction.model_copy(
        update={
            "candidates": [
                prediction.candidates[0].model_copy(
                    update={"evidence_ids": ["evidence-1", "evidence-1"]}
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="duplicate"):
        export_evidence_pairs([duplicate_reference], labels_visible=False)

    empty = prediction.model_copy(update={"candidates": []})
    export = export_evidence_pairs([empty], labels_visible=False)
    with pytest.raises(ValueError, match="empty"):
        freeze_manual_audit(export, [])


def test_audit_rejects_export_or_manual_artifact_changed_after_freeze():
    export = export_evidence_pairs([_prediction()], labels_visible=False)
    changed_export = export.model_copy(
        update={"pairs": [export.pairs[0].model_copy(update={"evidence_summary": "changed"})]}
    )
    decision = EvidenceAuditDecision(
        pair_id=changed_export.pairs[0].pair_id,
        entity_supported=True,
        temporally_compatible=True,
        mechanism_relevant=True,
        not_contradicted=True,
        reason_code="supported",
        reviewer_id="reviewer-project-owner",
        reviewed_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="export changed"):
        freeze_manual_audit(changed_export, [decision])


def test_prelabel_audit_binds_exact_prediction_bundle_hashes():
    export = export_evidence_pairs(
        [_prediction()],
        labels_visible=False,
        prediction_bundle_hashes={RcaEvalConfiguration.MULTI_INTENDED: "a" * 64},
    )
    decisions = [
        EvidenceAuditDecision(
            pair_id=pair.pair_id,
            entity_supported=True,
            temporally_compatible=True,
            mechanism_relevant=True,
            not_contradicted=True,
            reason_code="supported",
            reviewer_id="reviewer-project-owner",
            reviewed_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        for pair in export.pairs
    ]
    manual = freeze_manual_audit(export, decisions)

    with pytest.raises(ValueError, match="bundle|pre-label|audit"):
        validate_prelabel_audit(
            export,
            manual,
            expected_bundle_hashes={RcaEvalConfiguration.MULTI_INTENDED: "b" * 64},
            frozen_bundles={
                RcaEvalConfiguration.MULTI_INTENDED: PredictionBundle.model_construct(
                    configuration=RcaEvalConfiguration.MULTI_INTENDED,
                    predictions=[
                        _prediction().model_copy(
                            update={
                                "candidates": [
                                    _prediction().candidates[0].model_copy(
                                        update={"affected_service": "forged"}
                                    )
                                ]
                            }
                        )
                    ],
                    bundle_hash="a" * 64,
                )
            },
        )

    forged_bundle = PredictionBundle.model_construct(
        configuration=RcaEvalConfiguration.MULTI_INTENDED,
        predictions=[
            _prediction().model_copy(
                update={
                    "candidates": [
                        _prediction().candidates[0].model_copy(
                            update={"affected_service": "forged"}
                        )
                    ]
                }
            )
        ],
        bundle_hash="a" * 64,
    )
    with pytest.raises(ValueError, match="frozen prediction bundle|candidates/evidence"):
        validate_prelabel_audit(
            export,
            manual,
            expected_bundle_hashes={RcaEvalConfiguration.MULTI_INTENDED: "a" * 64},
            frozen_bundles={RcaEvalConfiguration.MULTI_INTENDED: forged_bundle},
        )
