import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.benchmarks.rcaeval.__main__ import _accept, _freeze_policy
from backend.benchmarks.rcaeval.audit import export_evidence_pairs, freeze_manual_audit
from backend.benchmarks.rcaeval.evaluator import (
    evaluate_acceptance,
    evaluate_bundles,
    evaluate_predictions,
    freeze_acceptance_policy,
    freeze_acceptance_result,
    mcnemar_exact,
    paired_bootstrap,
    scorer_dependency_hash,
)
from backend.benchmarks.rcaeval.models import (
    AcceptancePolicy,
    AcceptanceResultArtifact,
    CandidatePrediction,
    CasePrediction,
    EndpointCapabilityIdentity,
    EvaluationBudget,
    EvidenceAuditDecision,
    FrozenRunIdentity,
    LabelEntry,
    LabelManifest,
    PredictionBundle,
    RcaEvalConfiguration,
    RcaEvalPartition,
    RcaEvalSystem,
)
from backend.benchmarks.rcaeval.runner import (
    freeze_prediction_bundle,
    freeze_prediction_set,
)


def _label(
    case_id: str,
    service: str,
    fault: str,
    *,
    partition: RcaEvalPartition = RcaEvalPartition.SS30,
) -> LabelEntry:
    return LabelEntry(
        case_id=case_id,
        partition=partition,
        source_case_id=f"source-{case_id}",
        system=(
            RcaEvalSystem.TRAIN_TICKET
            if partition == RcaEvalPartition.TT90
            else RcaEvalSystem.SOCK_SHOP
        ),
        service=service,
        fault=fault,
        repetition=1,
    )


def _prediction(
    case_id: str,
    service: str | None,
    fault: str | None,
    *,
    completed: bool = True,
) -> CasePrediction:
    candidates = []
    if service is not None and fault is not None:
        candidates.append(
            CandidatePrediction(
                affected_service=service,
                failure_mechanism=fault,
                evidence_ids=[f"evidence-{case_id}"],
            )
        )
    return CasePrediction(
        case_id=case_id,
        configuration=RcaEvalConfiguration.MULTI_INTENDED,
        completed=completed,
        candidates=candidates,
        runtime_run_id=f"run-{case_id}",
        execution_contract_hash="a" * 64,
        available_evidence_ids=[f"evidence-{case_id}"],
        duration_ms=1000,
        input_tokens=10,
        output_tokens=5,
    )


def test_evaluator_reproduces_exact_top1_supporting_metrics_and_failure_semantics():
    labels = [
        _label("re2-aaaaaaaaaaaaaaaa", "checkout-service", "cpu stress"),
        _label("re2-bbbbbbbbbbbbbbbb", "cart", "network delay"),
        _label("re2-cccccccccccccccc", "payment", "memory leak"),
    ]
    predictions = [
        _prediction("re2-aaaaaaaaaaaaaaaa", " Checkout_Service ", "CPU-STRESS"),
        _prediction("re2-bbbbbbbbbbbbbbbb", "cart", "wrong"),
        _prediction(
            "re2-cccccccccccccccc",
            "payment",
            "memory leak",
            completed=False,
        ),
    ]

    summary = evaluate_predictions(predictions, labels)

    assert summary.case_count == 3
    assert summary.exact_top1 == pytest.approx(1 / 3)
    assert summary.component_top1 == pytest.approx(2 / 3)
    assert summary.mechanism_top1 == pytest.approx(1 / 3)
    assert summary.top3 == pytest.approx(1 / 3)
    assert summary.completion_rate == pytest.approx(2 / 3)
    assert summary.reference_integrity == 1
    assert summary.input_tokens == 30
    assert summary.output_tokens == 15
    assert summary.p95_latency_ms == 1000


def test_evaluator_rejects_duplicate_missing_and_non_finite_rows():
    label = _label("re2-aaaaaaaaaaaaaaaa", "checkout", "cpu")
    prediction = _prediction(label.case_id, "checkout", "cpu")
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_predictions([prediction, prediction], [label])
    with pytest.raises(ValueError, match="case set"):
        evaluate_predictions([], [label])
    invalid = prediction.model_copy(update={"duration_ms": math.inf})
    with pytest.raises(ValueError, match="finite|valid"):
        evaluate_predictions([invalid], [label])


def test_evaluator_rejects_prediction_identity_when_scorer_dependency_closure_is_stale():
    assert len(scorer_dependency_hash()) == 64


def test_evaluator_rejects_frozen_bundles_with_stale_scorer_dependency_identity(
    tmp_path: Path,
):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(
        runtime_manifest_hash="2" * 64,
        entries=_formal_labels(label),
    )
    labels.manifest_hash = _manifest_hash(labels)
    stale_identity = single.identity.model_copy(
        update={"scorer_dependency_hash": "e" * 64}
    )
    stale_bundles = []
    for bundle in (single, multi):
        changed = bundle.model_copy(update={"identity": stale_identity})
        changed.bundle_hash = _bundle_hash(changed)
        stale_bundles.append(changed)

    with pytest.raises(ValueError, match="scorer dependency"):
        evaluate_bundles(stale_bundles, labels)


def test_formal_configuration_contract_rejects_swapped_topology_limits(tmp_path: Path):
    from backend.benchmarks.rcaeval.evaluator import _validate_formal_configuration_set

    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction("re2-aaaaaaaaaaaaaaaa", "checkout", "cpu"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction("re2-aaaaaaaaaaaaaaaa", "checkout", "cpu"),
    )
    swapped = multi.model_copy(
        update={
            "budget": multi.budget.model_copy(
                update={"max_investigators": 1, "max_rounds": 1}
            )
        }
    )
    with pytest.raises(ValueError, match="topology|investigator|round"):
        _validate_formal_configuration_set(
            "tt90",
            {
                RcaEvalConfiguration.SINGLE_INTENDED: single,
                RcaEvalConfiguration.MULTI_INTENDED: swapped,
            },
        )


def test_acceptance_policy_rejects_self_consistent_stale_scorer_identity(
    tmp_path: Path,
):
    sealed = _synthetic_ss30_artifact(tmp_path)
    stale_identity = sealed.frozen_identity.model_copy(
        update={"scorer_dependency_hash": "e" * 64}
    )
    stale = sealed.model_copy(update={"frozen_identity": stale_identity})
    stale.artifact_hash = _artifact_hash(stale)
    with pytest.raises(ValueError, match="scorer dependency"):
        freeze_acceptance_policy(
            sealed_validation=stale,
            tt90_manifest_hash="f" * 64,
        )


def test_prelabel_audit_binds_candidate_onset_window(tmp_path: Path):
    prediction = _prediction("re2-aaaaaaaaaaaaaaaa", "checkout", "cpu")
    with_onset = prediction.model_copy(
        update={
            "candidates": [
                prediction.candidates[0].model_copy(
                    update={
                        "onset_window_start": datetime(2026, 8, 1, tzinfo=UTC),
                        "onset_window_end": datetime(2026, 8, 2, tzinfo=UTC),
                    }
                )
            ]
        }
    )
    frozen = _frozen_bundle(tmp_path, RcaEvalConfiguration.MULTI_INTENDED, with_onset)
    forged_prediction = with_onset.model_copy(
        update={
            "candidates": [
                with_onset.candidates[0].model_copy(
                    update={"onset_window_start": None, "onset_window_end": None}
                )
            ]
        }
    )
    forged = frozen.model_copy(update={"predictions": [forged_prediction]})
    forged.bundle_hash = _bundle_hash(forged)
    export = export_evidence_pairs(
        [with_onset],
        labels_visible=False,
        prediction_bundle_hashes={RcaEvalConfiguration.MULTI_INTENDED: forged.bundle_hash},
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
    with pytest.raises(ValueError, match="candidate|onset|evidence"):
        from backend.benchmarks.rcaeval.audit import validate_prelabel_audit

        validate_prelabel_audit(
            export,
            manual,
            expected_bundle_hashes={
                RcaEvalConfiguration.MULTI_INTENDED: forged.bundle_hash
            },
            frozen_bundles={RcaEvalConfiguration.MULTI_INTENDED: forged},
        )


def test_final_acceptance_rejects_self_consistent_stale_scorer_identity(
    tmp_path: Path,
):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "wrong", "wrong"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(
        runtime_manifest_hash="2" * 64,
        entries=_formal_labels(label),
    )
    labels.manifest_hash = _manifest_hash(labels)
    artifact = evaluate_bundles([single, multi], labels)
    policy = freeze_acceptance_policy(
        sealed_validation=_sealed_validation(artifact),
        tt90_manifest_hash="2" * 64,
    )
    stale_identity = artifact.frozen_identity.model_copy(
        update={"scorer_dependency_hash": "e" * 64}
    )
    stale_artifact = artifact.model_copy(update={"frozen_identity": stale_identity})
    stale_artifact.artifact_hash = _artifact_hash(stale_artifact)
    stale_policy = policy.model_copy(update={"frozen_identity": stale_identity})
    stale_policy.policy_hash = hashlib.sha256(
        json.dumps(
            stale_policy.model_dump(mode="json", exclude={"policy_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    audit_export, manual_audit = _passing_audit(multi)
    with pytest.raises(ValueError, match="scorer dependency"):
        evaluate_acceptance(
            stale_artifact,
            stale_policy,
            audit_export=audit_export,
            manual_audit=manual_audit,
            frozen_bundles=[multi],
        )


def test_paired_bootstrap_and_mcnemar_are_frozen_and_exact():
    single = [False, False, True, False]
    multi = [True, False, True, True]

    bootstrap = paired_bootstrap(single, multi)

    assert bootstrap.seed == 20260802
    assert bootstrap.samples == 10_000
    assert bootstrap.observed_delta == pytest.approx(0.5)
    assert len(bootstrap.indices_hash) == 64
    assert bootstrap.ci_low <= bootstrap.observed_delta <= bootstrap.ci_high
    assert mcnemar_exact(single, multi).discordant_single_only == 0
    assert mcnemar_exact(single, multi).discordant_multi_only == 2
    assert mcnemar_exact(single, multi).p_value == pytest.approx(0.5)


def _identity() -> FrozenRunIdentity:
    dependency_hash = scorer_dependency_hash()
    return FrozenRunIdentity(
        source_commit="1" * 40,
        source_manifest_hash="c" * 64,
        runtime_manifest_hash="2" * 64,
        capability=EndpointCapabilityIdentity(
            provider="deepseek",
            model="frozen-model",
            api_mode="chat_completions",
            endpoint_id="endpoint",
            artifact_hash="3" * 64,
        ),
        prompt_hash="4" * 64,
        tool_manifest_hash="5" * 64,
        skill_catalog_hash="6" * 64,
        prediction_schema_hash="7" * 64,
        normalizer_hash="8" * 64,
        scorer_dependency_hash=dependency_hash,
        dependency_lock_hash="9" * 64,
        memory_snapshot_hash="a" * 64,
        retry_policy_hash="b" * 64,
    )


def _frozen_bundle(
    tmp_path: Path,
    configuration: RcaEvalConfiguration,
    prediction: CasePrediction,
    *,
    case_count: int = 90,
) -> PredictionBundle:
    budget = EvaluationBudget(
        configuration=configuration,
        token_budget=4_000 if not configuration.is_multi else 10_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
        max_investigators=3 if configuration.is_multi else 1,
        max_rounds=2 if configuration.is_multi else 1,
    )
    bundle = PredictionBundle(
        partition=RcaEvalPartition.TT90,
        configuration=configuration,
        identity=_identity(),
        budget=budget,
        predictions=[
            _prediction_for_case(
                prediction,
                configuration=configuration,
                case_id=_formal_case_id(index, prediction.case_id),
            )
            for index in range(case_count)
        ],
        frozen_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    directory = tmp_path / configuration.value
    freeze_prediction_bundle(bundle, directory)
    return PredictionBundle.model_validate_json(
        (directory / "predictions.json").read_text(encoding="utf-8")
    )


def _formal_case_id(index: int, first_case_id: str) -> str:
    return first_case_id if index == 0 else f"re2-{index:016x}"


def _prediction_for_case(
    prediction: CasePrediction,
    *,
    configuration: RcaEvalConfiguration,
    case_id: str,
) -> CasePrediction:
    evidence_id = f"evidence-{case_id}"
    candidates = [
        candidate.model_copy(update={"evidence_ids": [evidence_id]})
        for candidate in prediction.candidates
    ]
    return prediction.model_copy(
        update={
            "case_id": case_id,
            "configuration": configuration,
            "runtime_run_id": f"run-{case_id}",
            "available_evidence_ids": [evidence_id],
            "evidence_summaries": {evidence_id: "bounded evidence"},
            "candidates": candidates,
        }
    )


def _formal_labels(
    first: LabelEntry,
    *,
    count: int = 90,
) -> list[LabelEntry]:
    return [
        first.model_copy(
            update={
                "case_id": _formal_case_id(index, first.case_id),
                "source_case_id": f"source-{_formal_case_id(index, first.case_id)}",
            }
        )
        for index in range(count)
    ]


def test_frozen_bundle_evaluation_and_acceptance_policy(tmp_path: Path):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    # custodian root 必须是专属目录：以共享 pytest 基目录为 root 会把其他测试的
    # 临时 entries 纳入 custodian 递归检查面。
    pair_root = tmp_path / "pair"
    single = _frozen_bundle(
        pair_root,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "wrong", "wrong"),
    )
    multi = _frozen_bundle(
        pair_root,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(
        runtime_manifest_hash="2" * 64,
        entries=_formal_labels(label),
    )
    label_payload = labels.model_dump(mode="json", exclude={"manifest_hash"})
    label_hash = hashlib.sha256(
        json.dumps(
            label_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    labels = labels.model_copy(update={"manifest_hash": label_hash})

    artifact = evaluate_bundles([single, multi], labels)
    sealed_validation = _sealed_validation(artifact, multi_exact=0.8)
    policy = freeze_acceptance_policy(
        sealed_validation=sealed_validation,
        tt90_manifest_hash="2" * 64,
    )
    audit_export, manual_audit = _passing_audit(multi)
    decision = evaluate_acceptance(
        artifact,
        policy,
        audit_export=audit_export,
        manual_audit=manual_audit,
        frozen_bundles=[multi],
    )

    assert artifact.summaries[RcaEvalConfiguration.MULTI_INTENDED].exact_top1 == 1
    assert artifact.frozen_identity == _identity()
    assert artifact.paired[0].bootstrap.observed_delta == 1
    assert policy.final_exact_gate == pytest.approx(0.7)
    assert policy.sealed_validation_artifact_hash == sealed_validation.artifact_hash
    assert decision.passed is True
    result = freeze_acceptance_result(
        artifact,
        policy,
        audit_export=audit_export,
        manual_audit=manual_audit,
        frozen_bundles=[multi],
    )
    assert result.decision == decision
    assert result.evaluation_artifact_hash == artifact.artifact_hash
    assert result.policy_hash == policy.policy_hash
    assert result.manual_audit_artifact_hash == manual_audit.artifact_hash
    assert len(result.artifact_hash) == 64
    tampered = multi.model_copy(
        update={"identity": multi.identity.model_copy(update={"prompt_hash": "d" * 64})}
    )
    with pytest.raises(ValueError, match="mixed|changed"):
        evaluate_bundles([single, tampered], labels)

    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        create_custodian_manifest,
    )

    manifest = create_custodian_manifest(
        tmp_path,
        runtime_manifest_hash="2" * 64,
        label_manifest_hash="2" * 64,
    )
    ledger = CustodianPairLedger.from_manifest(manifest)
    expected_configurations = {
        RcaEvalConfiguration.SINGLE_INTENDED,
        RcaEvalConfiguration.MULTI_INTENDED,
    }
    pair_identity = hashlib.sha256(
        json.dumps(
            {
                "partition": "tt90",
                "runtime_manifest_hash": "2" * 64,
                "capability_artifact_hash": "3" * 64,
                "source_revision": "1" * 40,
                "source_manifest_hash": "c" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    ledger.initialize(
        partition="tt90",
        prediction_set_hash=pair_identity,
        expected_sides=tuple(item.value for item in expected_configurations),
    )
    for configuration in sorted(expected_configurations, key=lambda item: item.value):
        side_dir = pair_root / configuration.value
        lease = ledger.record_side_started(configuration.value, str(side_dir))
        ledger.record_side_completed(
            configuration.value,
            hashlib.sha256((side_dir / "SHA256SUMS").read_bytes()).hexdigest(),
            lease_token=lease,
        )

    set_hash = freeze_prediction_set(
        pair_root,
        expected_configurations=expected_configurations,
        expected_case_count=90,
        ledger=ledger,
    )
    assert len(set_hash) == 64
    assert freeze_prediction_set(
        pair_root,
        expected_configurations=expected_configurations,
        expected_case_count=90,
        ledger=ledger,
    ) == set_hash
    ledger.bind_prediction_set_hash(set_hash)
    assert freeze_prediction_set(
        pair_root,
        expected_configurations=expected_configurations,
        expected_case_count=90,
        ledger=ledger,
    ) == set_hash


def test_formal_tt90_rejects_one_case_bundle_before_scoring(tmp_path: Path):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
        case_count=1,
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
        case_count=1,
    )
    labels = LabelManifest(runtime_manifest_hash="2" * 64, entries=[label])
    labels.manifest_hash = _manifest_hash(labels)

    with pytest.raises(ValueError, match="90|cardinality|case count"):
        evaluate_bundles([single, multi], labels)


def test_acceptance_freeze_rejects_nonformal_summary_cardinality(tmp_path: Path):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "wrong", "wrong"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(
        runtime_manifest_hash="2" * 64,
        entries=_formal_labels(label),
    )
    labels.manifest_hash = _manifest_hash(labels)
    artifact = evaluate_bundles([single, multi], labels)
    malformed = _sealed_validation(artifact).model_copy(
        update={
            "summaries": {
                **_sealed_validation(artifact).summaries,
                RcaEvalConfiguration.MULTI_INTENDED: artifact.summaries[
                    RcaEvalConfiguration.MULTI_INTENDED
                ].model_copy(update={"case_count": 1}),
            }
        }
    )
    malformed.artifact_hash = _artifact_hash(malformed)

    with pytest.raises(ValueError, match="cardinality|30"):
        freeze_acceptance_policy(
            sealed_validation=malformed,
            tt90_manifest_hash="2" * 64,
        )


def test_acceptance_rejects_tampered_policy_wrong_partition_and_mixed_identity(
    tmp_path: Path,
):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "wrong", "wrong"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(runtime_manifest_hash="2" * 64, entries=_formal_labels(label))
    labels.manifest_hash = hashlib.sha256(
        json.dumps(
            labels.model_dump(mode="json", exclude={"manifest_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    artifact = evaluate_bundles([single, multi], labels)
    sealed_validation = _sealed_validation(artifact)
    policy = freeze_acceptance_policy(
        sealed_validation=sealed_validation,
        tt90_manifest_hash="2" * 64,
    )

    with pytest.raises(ValueError, match="policy changed"):
        evaluate_acceptance(
            artifact,
            policy.model_copy(update={"final_exact_gate": 0.95}),
            audit_export=_passing_audit(multi)[0],
            manual_audit=_passing_audit(multi)[1],
            frozen_bundles=[multi],
        )
    wrong_partition = artifact.model_copy(update={"partition": RcaEvalPartition.SS30})
    wrong_partition.artifact_hash = _artifact_hash(wrong_partition)
    with pytest.raises(ValueError, match="TT90"):
        evaluate_acceptance(
            wrong_partition,
            policy,
            audit_export=_passing_audit(multi)[0],
            manual_audit=_passing_audit(multi)[1],
            frozen_bundles=[multi],
        )
    wrong_identity = artifact.model_copy(
        update={
            "frozen_identity": artifact.frozen_identity.model_copy(
                update={"prompt_hash": "d" * 64}
            )
        }
    )
    wrong_identity.artifact_hash = _artifact_hash(wrong_identity)
    with pytest.raises(ValueError, match="identity"):
        evaluate_acceptance(
            wrong_identity,
            policy,
            audit_export=_passing_audit(multi)[0],
            manual_audit=_passing_audit(multi)[1],
            frozen_bundles=[multi],
        )

    wrong_configuration_set = artifact.model_copy(
        update={
            "summaries": {
                **artifact.summaries,
                RcaEvalConfiguration.MULTI_EQUAL_TOKEN: artifact.summaries[
                    RcaEvalConfiguration.MULTI_INTENDED
                ],
            },
            "prediction_bundle_hashes": {
                **artifact.prediction_bundle_hashes,
                RcaEvalConfiguration.MULTI_EQUAL_TOKEN: "d" * 64,
            },
        }
    )
    wrong_configuration_set.artifact_hash = _artifact_hash(wrong_configuration_set)
    with pytest.raises(ValueError, match="intended configurations"):
        evaluate_acceptance(
            wrong_configuration_set,
            policy,
            audit_export=_passing_audit(multi)[0],
            manual_audit=_passing_audit(multi)[1],
            frozen_bundles=[multi],
        )

    audit_export, manual_audit = _passing_audit(multi)
    with pytest.raises(ValueError, match="manual audit changed"):
        evaluate_acceptance(
            artifact,
            policy,
            audit_export=audit_export,
            manual_audit=manual_audit.model_copy(update={"pass_rate": 0.5}),
            frozen_bundles=[multi],
        )
    wrong_export = export_evidence_pairs(
        multi.predictions,
        labels_visible=False,
        prediction_bundle_hashes={RcaEvalConfiguration.MULTI_INTENDED: "f" * 64},
    )
    wrong_manual = freeze_manual_audit(wrong_export, manual_audit.decisions)
    with pytest.raises(ValueError, match="prediction bundle"):
        evaluate_acceptance(
            artifact,
            policy,
            audit_export=wrong_export,
            manual_audit=wrong_manual,
            frozen_bundles=[multi],
        )


def _artifact_hash(artifact) -> str:
    return hashlib.sha256(
        json.dumps(
            artifact.model_dump(mode="json", exclude={"artifact_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _bundle_hash(bundle: PredictionBundle) -> str:
    return hashlib.sha256(
        json.dumps(
            bundle.model_dump(mode="json", exclude={"bundle_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _manifest_hash(manifest: LabelManifest) -> str:
    return hashlib.sha256(
        json.dumps(
            manifest.model_dump(mode="json", exclude={"manifest_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sealed_validation(artifact, *, multi_exact: float | None = None):
    single_summary = artifact.summaries[RcaEvalConfiguration.SINGLE_INTENDED]
    multi_summary = artifact.summaries[RcaEvalConfiguration.MULTI_INTENDED]
    single_summary = single_summary.model_copy(update={"case_count": 30})
    multi_summary = multi_summary.model_copy(update={"case_count": 30})
    if multi_exact is not None:
        multi_summary = multi_summary.model_copy(update={"exact_top1": multi_exact})
    intended_pair = artifact.paired[0]
    sealed = artifact.model_copy(
        update={
            "partition": RcaEvalPartition.SS30,
            "prediction_bundle_hashes": {
                **artifact.prediction_bundle_hashes,
                RcaEvalConfiguration.SINGLE_EQUAL_TOKEN: "c" * 64,
                RcaEvalConfiguration.MULTI_EQUAL_TOKEN: "d" * 64,
            },
            "summaries": {
                RcaEvalConfiguration.SINGLE_INTENDED: single_summary,
                RcaEvalConfiguration.MULTI_INTENDED: multi_summary,
                RcaEvalConfiguration.SINGLE_EQUAL_TOKEN: single_summary,
                RcaEvalConfiguration.MULTI_EQUAL_TOKEN: multi_summary,
            },
            "paired": [
                intended_pair,
                intended_pair.model_copy(
                    update={
                        "single_configuration": (
                            RcaEvalConfiguration.SINGLE_EQUAL_TOKEN
                        ),
                        "multi_configuration": RcaEvalConfiguration.MULTI_EQUAL_TOKEN,
                    }
                ),
            ],
        }
    )
    sealed.artifact_hash = _artifact_hash(sealed)
    return sealed


def _passing_audit(bundle: PredictionBundle):
    export = export_evidence_pairs(
        bundle.predictions,
        labels_visible=False,
        prediction_bundle_hashes={bundle.configuration: bundle.bundle_hash},
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
    return export, freeze_manual_audit(export, decisions)


def _synthetic_ss30_artifact(tmp_path: Path):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(
        runtime_manifest_hash="2" * 64,
        entries=_formal_labels(label),
    )
    labels.manifest_hash = _manifest_hash(labels)
    return _sealed_validation(evaluate_bundles([single, multi], labels))


def test_cli_freezes_policy_and_archives_tt90_acceptance(tmp_path: Path):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path / "bundles",
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "wrong", "wrong"),
    )
    multi = _frozen_bundle(
        tmp_path / "bundles",
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(runtime_manifest_hash="2" * 64, entries=_formal_labels(label))
    labels.manifest_hash = hashlib.sha256(
        json.dumps(
            labels.model_dump(mode="json", exclude={"manifest_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    tt90 = evaluate_bundles([single, multi], labels)
    ss30 = _sealed_validation(tt90)
    audit_export, manual_audit = _passing_audit(multi)
    paths = {
        "sealed": tmp_path / "ss30-evaluation.json",
        "policy": tmp_path / "acceptance-policy.json",
        "tt90": tmp_path / "tt90-evaluation.json",
        "export": tmp_path / "evidence-audit-export.json",
        "audit": tmp_path / "manual-audit.json",
        "result": tmp_path / "acceptance-result.json",
    }
    for key, artifact in (
        ("sealed", ss30),
        ("tt90", tt90),
        ("export", audit_export),
        ("audit", manual_audit),
    ):
        paths[key].write_text(artifact.model_dump_json(), encoding="utf-8")

    _freeze_policy(
        SimpleNamespace(
            sealed_validation=paths["sealed"],
            tt90_manifest_hash="2" * 64,
            output=paths["policy"],
        )
    )
    policy = AcceptancePolicy.model_validate_json(
        paths["policy"].read_text(encoding="utf-8")
    )
    _accept(
        SimpleNamespace(
            evaluation=paths["tt90"],
            policy=paths["policy"],
            audit_export=paths["export"],
            manual_audit=paths["audit"],
            bundle=[tmp_path / "bundles" / "multi_intended" / "predictions.json"],
            output=paths["result"],
        )
    )
    result = AcceptanceResultArtifact.model_validate_json(
        paths["result"].read_text(encoding="utf-8")
    )

    assert policy.sealed_validation_artifact_hash == ss30.artifact_hash
    assert result.decision.passed is True


def test_policy_rejects_incomplete_ss30_artifact(tmp_path: Path):
    label = _label(
        "re2-aaaaaaaaaaaaaaaa",
        "checkout",
        "cpu",
        partition=RcaEvalPartition.TT90,
    )
    single = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.SINGLE_INTENDED,
        _prediction(label.case_id, "wrong", "wrong"),
    )
    multi = _frozen_bundle(
        tmp_path,
        RcaEvalConfiguration.MULTI_INTENDED,
        _prediction(label.case_id, "checkout", "cpu"),
    )
    labels = LabelManifest(runtime_manifest_hash="2" * 64, entries=_formal_labels(label))
    labels.manifest_hash = hashlib.sha256(
        json.dumps(
            labels.model_dump(mode="json", exclude={"manifest_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    incomplete = evaluate_bundles([single, multi], labels).model_copy(
        update={"partition": RcaEvalPartition.SS30}
    )
    incomplete.artifact_hash = _artifact_hash(incomplete)

    with pytest.raises(ValueError, match="four configurations"):
        freeze_acceptance_policy(
            sealed_validation=incomplete,
            tt90_manifest_hash="2" * 64,
        )
