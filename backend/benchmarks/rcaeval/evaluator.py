"""RCAEval 标签侧 evaluator；本模块不得导入生产 runtime。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from backend.benchmarks.rcaeval.audit import validate_manual_audit, validate_prelabel_audit
from backend.benchmarks.rcaeval.dependency import (
    scorer_dependency_hash as _scorer_dependency_hash,
)
from backend.benchmarks.rcaeval.ledger import CustodianPairLedger
from backend.benchmarks.rcaeval.models import (
    EXPECTED_CONFIGURATION_TOPOLOGY,
    EXPECTED_PARTITION_COUNTS,
    AcceptanceDecision,
    AcceptancePolicy,
    AcceptanceResultArtifact,
    BootstrapResult,
    CandidatePrediction,
    CasePrediction,
    EvaluationArtifact,
    EvaluationSummary,
    EvidenceAuditExport,
    LabelEntry,
    LabelManifest,
    ManualAuditArtifact,
    McNemarResult,
    PairedEvaluation,
    PredictionBundle,
    RcaEvalConfiguration,
    canonical_json_sha256,
)

BOOTSTRAP_SEED = 20260802
BOOTSTRAP_SAMPLES = 10_000
FORMAL_CASE_COUNTS = {
    partition.value: count
    for partition, count in EXPECTED_PARTITION_COUNTS.items()
    if partition.value in {"ss15", "tt90"}
}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def scorer_dependency_hash() -> str:
    """Return the exact hash frozen into prediction and policy identities."""
    return _scorer_dependency_hash()


def normalize_label(value: str) -> str:
    """冻结基础 normalizer：大小写、空白和分隔符。"""
    return " ".join(re.sub(r"[-_/]+", " ", value.casefold()).split())


# 固定整词机制映射；症状不能映射为具体原因。此表受 scorer_dependency_hash
# 约束，变更后旧冻结评分不可混用；禁止按案例真值猜测自由文本的含义。
_RCA_EVAL_FAULT_ALIASES = {
    "cpu saturation": "cpu",
    "cpu exhaustion": "cpu",
    "cpu stress": "cpu",
    "memory": "mem",
    "memory pressure": "mem",
    "memory exhaustion": "mem",
    "memory leak": "mem",
    "socket exhaustion": "socket",
    "connection exhaustion": "socket",
    "connection pool exhaustion": "socket",
    "disk io": "disk",
    "disk io saturation": "disk",
    "disk i o saturation": "disk",
    "disk io contention": "disk",
    "disk i o contention": "disk",
    "network corruption": "loss",
    "network loss": "loss",
    "packet loss": "loss",
    "network latency": "delay",
    "network delay": "delay",
}


def normalize_fault_label(value: str) -> str:
    """按冻结的 RCAEval canonical-family 别名归一化 fault。"""
    normalized = normalize_label(value)
    return _RCA_EVAL_FAULT_ALIASES.get(normalized, normalized)


def _scored_failure_class(candidate: CandidatePrediction) -> str:
    """优先使用结构化分类；旧 bundle 仍以机制字段作为兼容输入。"""
    return candidate.failure_class or candidate.failure_mechanism


def evaluate_predictions(
    predictions: list[CasePrediction], labels: list[LabelEntry]
) -> EvaluationSummary:
    prediction_by_id = _unique_by_case(predictions, "prediction")
    label_by_id = _unique_by_case(labels, "label")
    if set(prediction_by_id) != set(label_by_id):
        raise ValueError("prediction and label case set differs")
    for item in predictions:
        if not math.isfinite(item.duration_ms):
            raise ValueError("prediction metrics must be finite and valid")

    exact = component = mechanism = top3 = completed = 0
    referenced = invalid_references = 0
    latencies: list[float] = []
    for case_id in sorted(label_by_id):
        prediction = prediction_by_id[case_id]
        label = label_by_id[case_id]
        completed += int(prediction.completed)
        latencies.append(prediction.duration_ms)
        top = prediction.candidates[0] if prediction.candidates else None
        service_match = bool(
            prediction.completed
            and top is not None
            and normalize_label(top.affected_service) == normalize_label(label.service)
        )
        mechanism_match = bool(
            prediction.completed
            and top is not None
            and normalize_fault_label(_scored_failure_class(top))
            == normalize_fault_label(label.fault)
        )
        component += int(service_match)
        mechanism += int(mechanism_match)
        exact += int(service_match and mechanism_match)
        top3 += int(
            prediction.completed
            and any(
                normalize_label(candidate.affected_service) == normalize_label(label.service)
                and normalize_fault_label(_scored_failure_class(candidate))
                == normalize_fault_label(label.fault)
                for candidate in prediction.candidates
            )
        )
        available = set(prediction.available_evidence_ids)
        for candidate in prediction.candidates:
            referenced += len(candidate.evidence_ids)
            invalid_references += sum(item not in available for item in candidate.evidence_ids)

    count = len(labels)
    reference_integrity = (
        None if referenced == 0 else (referenced - invalid_references) / referenced
    )
    return EvaluationSummary(
        case_count=count,
        exact_top1=exact / count if count else 0,
        component_top1=component / count if count else 0,
        mechanism_top1=mechanism / count if count else 0,
        top3=top3 / count if count else 0,
        completion_rate=completed / count if count else 0,
        reference_integrity=reference_integrity,
        p95_latency_ms=_nearest_rank_percentile(latencies, 0.95),
        input_tokens=sum(item.input_tokens for item in predictions),
        output_tokens=sum(item.output_tokens for item in predictions),
        tool_calls=sum(item.tool_calls for item in predictions),
        read_only_violations=sum(item.read_only_violations for item in predictions),
        leakage_violations=sum(item.leakage_violations for item in predictions),
        failed_cases=sum(not item.completed for item in predictions),
        failure_counts={
            category: sum(item.failure_category == category for item in predictions)
            for category in sorted(
                {
                    item.failure_category
                    for item in predictions
                    if item.failure_category is not None
                }
            )
        },
    )


def paired_bootstrap(
    single_correct: list[bool], multi_correct: list[bool]
) -> BootstrapResult:
    if not single_correct or len(single_correct) != len(multi_correct):
        raise ValueError("paired bootstrap requires equal non-empty inputs")
    rng = random.Random(BOOTSTRAP_SEED)
    size = len(single_correct)
    indices = [[rng.randrange(size) for _ in range(size)] for _ in range(BOOTSTRAP_SAMPLES)]
    deltas = [
        sum(int(multi_correct[index]) - int(single_correct[index]) for index in sample) / size
        for sample in indices
    ]
    canonical = json.dumps(indices, separators=(",", ":")).encode("utf-8")
    return BootstrapResult(
        seed=BOOTSTRAP_SEED,
        samples=BOOTSTRAP_SAMPLES,
        observed_delta=(sum(multi_correct) - sum(single_correct)) / size,
        ci_low=_linear_percentile(deltas, 0.025),
        ci_high=_linear_percentile(deltas, 0.975),
        indices_hash=hashlib.sha256(canonical).hexdigest(),
    )


def mcnemar_exact(single_correct: list[bool], multi_correct: list[bool]) -> McNemarResult:
    if not single_correct or len(single_correct) != len(multi_correct):
        raise ValueError("McNemar requires equal non-empty inputs")
    pairs = list(zip(single_correct, multi_correct, strict=True))
    single_only = sum(left and not right for left, right in pairs)
    multi_only = sum(right and not left for left, right in pairs)
    discordant = single_only + multi_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, index)
            for index in range(min(single_only, multi_only) + 1)
        )
        p_value = min(1.0, 2.0 * tail / (2**discordant))
    return McNemarResult(
        discordant_single_only=single_only,
        discordant_multi_only=multi_only,
        p_value=p_value,
    )


def evaluate_bundles(
    bundles: list[PredictionBundle],
    label_manifest: LabelManifest,
) -> EvaluationArtifact:
    if not bundles:
        raise ValueError("evaluation requires at least one prediction bundle")
    actual_label_hash = canonical_json_sha256(
        label_manifest.model_dump(mode="json", exclude={"manifest_hash"})
    )
    if label_manifest.manifest_hash != actual_label_hash:
        raise ValueError("label manifest changed after custodian freeze")
    by_configuration = {bundle.configuration: bundle for bundle in bundles}
    if len(by_configuration) != len(bundles):
        raise ValueError("evaluation contains a duplicate configuration")
    partitions = {bundle.partition for bundle in bundles}
    identities = {
        json.dumps(
            bundle.identity.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        for bundle in bundles
    }
    if len(partitions) != 1 or len(identities) != 1:
        raise ValueError("evaluation bundles contain mixed partition or identity")
    partition = bundles[0].partition
    _validate_formal_configuration_set(partition.value, by_configuration)
    for bundle in bundles:
        _validate_frozen_bundle(bundle)
    identity = bundles[0].identity
    _validate_scorer_dependency_identity(bundles)
    if label_manifest.runtime_manifest_hash != identity.runtime_manifest_hash:
        raise ValueError("evaluation label/runtime binding mismatch")
    labels = [item for item in label_manifest.entries if item.partition == partition]
    expected_case_count = FORMAL_CASE_COUNTS[partition.value]
    if len(labels) != expected_case_count or len(
        {item.case_id for item in labels}
    ) != expected_case_count:
        raise ValueError("formal evaluation label cardinality is not frozen")
    for bundle in by_configuration.values():
        _validate_formal_bundle_cardinality(bundle, expected_case_count)
    summaries = {
        configuration: evaluate_predictions(bundle.predictions, labels)
        for configuration, bundle in by_configuration.items()
    }
    paired: list[PairedEvaluation] = []
    for single_name, multi_name in (
        (
            RcaEvalConfiguration.SINGLE_INTENDED,
            RcaEvalConfiguration.MULTI_INTENDED,
        ),
        (
            RcaEvalConfiguration.SINGLE_EQUAL_TOKEN,
            RcaEvalConfiguration.MULTI_EQUAL_TOKEN,
        ),
    ):
        if single_name not in by_configuration or multi_name not in by_configuration:
            continue
        single = by_configuration[single_name]
        multi = by_configuration[multi_name]
        single_correct = _correct_vector(single.predictions, labels)
        multi_correct = _correct_vector(multi.predictions, labels)
        single_tokens = _billable_tokens(single.predictions)
        multi_tokens = _billable_tokens(multi.predictions)
        ratio = None if single_tokens == 0 else multi_tokens / single_tokens
        paired.append(
            PairedEvaluation(
                single_configuration=single_name,
                multi_configuration=multi_name,
                token_ratio=ratio,
                bootstrap=paired_bootstrap(single_correct, multi_correct),
                mcnemar=mcnemar_exact(single_correct, multi_correct),
            )
        )
    artifact = EvaluationArtifact(
        partition=partition,
        frozen_identity=identity,
        runtime_manifest_hash=identity.runtime_manifest_hash,
        labels_manifest_hash=label_manifest.manifest_hash,
        prediction_bundle_hashes={
            name: bundle.bundle_hash for name, bundle in by_configuration.items()
        },
        summaries=summaries,
        paired=paired,
        evaluated_at=datetime.now(UTC),
    )
    artifact.artifact_hash = canonical_json_sha256(
        artifact.model_dump(mode="json", exclude={"artifact_hash"})
    )
    return artifact


def freeze_acceptance_policy(
    *,
    sealed_validation: EvaluationArtifact,
    tt90_manifest_hash: str,
) -> AcceptancePolicy:
    if sealed_validation.partition.value != "ss15":
        raise ValueError("acceptance policy requires the sealed SS15 evaluation")
    _validate_artifact_hash(sealed_validation)
    _validate_scorer_dependency_identity(
        sealed_validation.frozen_identity.scorer_dependency_hash
    )
    expected_configurations = set(RcaEvalConfiguration)
    if (
        set(sealed_validation.summaries) != expected_configurations
        or set(sealed_validation.prediction_bundle_hashes) != expected_configurations
    ):
        raise ValueError("acceptance policy requires all four configurations")
    if any(
        summary.case_count != FORMAL_CASE_COUNTS["ss15"]
        for summary in sealed_validation.summaries.values()
    ):
        raise ValueError("acceptance policy SS15 cardinality is not frozen at 15")
    expected_pairs = {
        (
            RcaEvalConfiguration.SINGLE_INTENDED,
            RcaEvalConfiguration.MULTI_INTENDED,
        ),
        (
            RcaEvalConfiguration.SINGLE_EQUAL_TOKEN,
            RcaEvalConfiguration.MULTI_EQUAL_TOKEN,
        ),
    }
    if {
        (item.single_configuration, item.multi_configuration)
        for item in sealed_validation.paired
    } != expected_pairs:
        raise ValueError("acceptance policy requires both SS15 paired statistics")
    if (
        sealed_validation.frozen_identity.runtime_manifest_hash
        != sealed_validation.runtime_manifest_hash
    ):
        raise ValueError("sealed validation runtime identity mismatch")
    sealed_validation_multi_exact = sealed_validation.summaries[
        RcaEvalConfiguration.MULTI_INTENDED
    ].exact_top1
    policy = AcceptancePolicy(
        sealed_validation_intended_budget_multi_agent_exact=(
            sealed_validation_multi_exact
        ),
        sealed_validation_artifact_hash=sealed_validation.artifact_hash,
        final_exact_gate=max(0.60, sealed_validation_multi_exact - 0.10),
        frozen_identity=sealed_validation.frozen_identity,
        tt90_manifest_hash=tt90_manifest_hash,
        evaluator_hash=scorer_dependency_hash(),
    )
    policy.policy_hash = canonical_json_sha256(
        policy.model_dump(mode="json", exclude={"policy_hash"})
    )
    return policy


def evaluate_acceptance(
    artifact: EvaluationArtifact,
    policy: AcceptancePolicy,
    *,
    audit_export: EvidenceAuditExport,
    manual_audit: ManualAuditArtifact,
    frozen_bundles: list[PredictionBundle] | None = None,
) -> AcceptanceDecision:
    if frozen_bundles is None:
        raise ValueError("frozen prediction bundles are required for acceptance")
    _validate_policy_hash(policy)
    _validate_artifact_hash(artifact)
    _validate_scorer_dependency_identity(
        artifact.frozen_identity.scorer_dependency_hash
    )
    if artifact.partition.value != "tt90":
        raise ValueError("acceptance evaluation requires the TT90 artifact")
    intended_configurations = {
        RcaEvalConfiguration.SINGLE_INTENDED,
        RcaEvalConfiguration.MULTI_INTENDED,
    }
    if (
        set(artifact.summaries) != intended_configurations
        or set(artifact.prediction_bundle_hashes) != intended_configurations
        or [
            (item.single_configuration, item.multi_configuration)
            for item in artifact.paired
        ]
        != [
            (
                RcaEvalConfiguration.SINGLE_INTENDED,
                RcaEvalConfiguration.MULTI_INTENDED,
            )
        ]
    ):
        raise ValueError("TT90 acceptance requires only the intended configurations")
    if any(
        summary.case_count != FORMAL_CASE_COUNTS["tt90"]
        for summary in artifact.summaries.values()
    ):
        raise ValueError("TT90 acceptance cardinality is not frozen at 90")
    if artifact.frozen_identity != policy.frozen_identity:
        raise ValueError("acceptance artifact identity differs from frozen policy")
    if artifact.runtime_manifest_hash != policy.tt90_manifest_hash:
        raise ValueError("acceptance artifact uses the wrong TT90 manifest")
    if policy.evaluator_hash != scorer_dependency_hash():
        raise ValueError("acceptance evaluator changed after policy freeze")
    _validate_scorer_dependency_identity(policy.frozen_identity.scorer_dependency_hash)
    by_configuration = {bundle.configuration: bundle for bundle in frozen_bundles}
    if set(by_configuration) != {RcaEvalConfiguration.MULTI_INTENDED}:
        raise ValueError("TT90 acceptance requires the frozen multi intended bundle")
    multi_bundle = by_configuration[RcaEvalConfiguration.MULTI_INTENDED]
    _validate_frozen_bundle(multi_bundle)
    validate_prelabel_audit(
        audit_export,
        manual_audit,
        expected_bundle_hashes={
            RcaEvalConfiguration.MULTI_INTENDED: artifact.prediction_bundle_hashes[
                RcaEvalConfiguration.MULTI_INTENDED
            ]
        },
        frozen_bundles=by_configuration,
    )
    evidence_support = validate_manual_audit(audit_export, manual_audit)
    expected_audit_binding = {
        RcaEvalConfiguration.MULTI_INTENDED: artifact.prediction_bundle_hashes[
            RcaEvalConfiguration.MULTI_INTENDED
        ]
    }
    if audit_export.prediction_bundle_hashes != expected_audit_binding:
        raise ValueError("manual audit belongs to a different prediction bundle")
    single = artifact.summaries[RcaEvalConfiguration.SINGLE_INTENDED]
    multi = artifact.summaries[RcaEvalConfiguration.MULTI_INTENDED]
    comparison = next(
        item
        for item in artifact.paired
        if item.single_configuration == RcaEvalConfiguration.SINGLE_INTENDED
    )
    delta = multi.exact_top1 - single.exact_top1
    gates = {
        "exact": multi.exact_top1 >= policy.final_exact_gate,
        "delta": delta >= policy.minimum_absolute_delta,
        "tokens": comparison.token_ratio is not None
        and comparison.token_ratio <= policy.maximum_token_ratio,
        "reference_integrity": multi.reference_integrity
        == policy.minimum_reference_integrity,
        "evidence_support": evidence_support is not None
        and evidence_support >= policy.minimum_evidence_support,
        "latency": multi.p95_latency_ms <= policy.maximum_p95_latency_ms,
        "read_only": multi.read_only_violations
        <= policy.maximum_read_only_violations,
        "leakage": multi.leakage_violations <= policy.maximum_leakage_violations,
    }
    return AcceptanceDecision(
        passed=all(gates.values()),
        gates=gates,
        measured_single_exact=single.exact_top1,
        measured_multi_exact=multi.exact_top1,
        measured_delta=delta,
        token_ratio=comparison.token_ratio,
        evidence_support=evidence_support,
    )


def freeze_acceptance_result(
    artifact: EvaluationArtifact,
    policy: AcceptancePolicy,
    *,
    audit_export: EvidenceAuditExport,
    manual_audit: ManualAuditArtifact,
    frozen_bundles: list[PredictionBundle] | None = None,
) -> AcceptanceResultArtifact:
    decision = evaluate_acceptance(
        artifact,
        policy,
        audit_export=audit_export,
        manual_audit=manual_audit,
        frozen_bundles=frozen_bundles,
    )
    result = AcceptanceResultArtifact(
        evaluation_artifact_hash=artifact.artifact_hash,
        policy_hash=policy.policy_hash,
        manual_audit_artifact_hash=manual_audit.artifact_hash,
        decision=decision,
        evaluated_at=datetime.now(UTC),
    )
    result.artifact_hash = canonical_json_sha256(
        result.model_dump(mode="json", exclude={"artifact_hash"})
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.benchmarks.rcaeval.evaluator")
    parser.add_argument("--bundle", type=Path, action="append", required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--custodian-manifest", type=Path, required=True)
    parser.add_argument("--partition", choices=("ss15", "tt90"), required=True)
    parser.add_argument("--prediction-set-hash", required=True)
    parser.add_argument("--audit-export", type=Path, required=True)
    parser.add_argument("--manual-audit", type=Path, required=True)
    parser.add_argument("--label-open-token", required=True)
    parser.add_argument("--expected-label-manifest-hash", required=True)
    parser.add_argument("--expected-runtime-manifest-hash", required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise ValueError("evaluation output already exists; labels will not be reopened")
    # H2: child 首个副作用前独立复验冻结 root——canonical SHA256SUMS bytes/hash
    # 与全部 bundle bytes；launch spec 创建后的任何替换都在此 fail closed。
    # 之后的 bundle 解析只消费已验证字节，verify→parse 之间不存在替换窗口。
    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    bundle_paths = [Path(path).resolve() for path in arguments.bundle]
    if not bundle_paths or any(path.name != "predictions.json" for path in bundle_paths):
        raise ValueError("evaluator bundles must be frozen predictions.json files")
    roots = {path.parent.parent for path in bundle_paths}
    if len(roots) != 1:
        raise ValueError("evaluator bundles must share one frozen prediction root")
    verified_files = verify_frozen_prediction_root(
        roots.pop(), arguments.prediction_set_hash
    )
    bundles = []
    for path in bundle_paths:
        relative = path.relative_to(path.parent.parent).as_posix()
        bundles.append(PredictionBundle.model_validate_json(verified_files[relative]))
    export = EvidenceAuditExport.model_validate_json(
        arguments.audit_export.read_text(encoding="utf-8")
    )
    manual = ManualAuditArtifact.model_validate_json(
        arguments.manual_audit.read_text(encoding="utf-8")
    )
    expected_bundle_hashes = {
        bundle.configuration: bundle.bundle_hash for bundle in bundles
    }
    validate_prelabel_audit(
        export,
        manual,
        expected_bundle_hashes=expected_bundle_hashes,
        frozen_bundles={bundle.configuration: bundle for bundle in bundles},
    )
    if any(bundle.partition.value != arguments.partition for bundle in bundles):
        raise ValueError("evaluator partition differs from frozen pair")
    ledger = CustodianPairLedger.from_manifest(arguments.custodian_manifest)
    if ledger.path != arguments.ledger.resolve():
        raise ValueError("evaluator ledger path is not the custodian canonical ledger")
    ledger.assert_label_open(
        partition=arguments.partition,
        prediction_set_hash=arguments.prediction_set_hash,
        audit_export_hash=export.export_hash,
        manual_audit_hash=manual.artifact_hash,
        lease_token=arguments.label_open_token,
        expected_label_manifest_hash=arguments.expected_label_manifest_hash,
    )
    # 标签文件在 evaluator 进程中只打开这一次；custodian 身份匹配前不构造
    # scorer 结果，也不写任何 evaluator artifact。
    labels = read_label_manifest_once(
        arguments.labels,
        expected_manifest_hash=arguments.expected_label_manifest_hash,
        expected_runtime_manifest_hash=arguments.expected_runtime_manifest_hash,
    )
    artifact = evaluate_bundles(bundles, labels)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8", newline="\n") as output:
        output.write(
            json.dumps(
                artifact.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def read_label_manifest_once(
    path: Path,
    *,
    expected_manifest_hash: str,
    expected_runtime_manifest_hash: str,
) -> LabelManifest:
    """Read and bind the sole label bytes before any result construction."""
    if not _SHA256_PATTERN.fullmatch(expected_manifest_hash):
        raise ValueError("expected label manifest hash must be a sha256 hex digest")
    if not _SHA256_PATTERN.fullmatch(expected_runtime_manifest_hash):
        raise ValueError("expected runtime manifest hash must be a sha256 hex digest")
    label_bytes = _read_label_bytes_once(path)
    try:
        labels = LabelManifest.model_validate_json(label_bytes)
    except (ValueError, TypeError) as exc:
        raise ValueError("label manifest is invalid") from exc
    if labels.runtime_manifest_hash != expected_runtime_manifest_hash:
        raise ValueError("label/runtime binding mismatch")
    if labels.manifest_hash != expected_manifest_hash:
        raise ValueError("label manifest identity differs from custodian freeze")
    if (
        canonical_json_sha256(labels.model_dump(mode="json", exclude={"manifest_hash"}))
        != labels.manifest_hash
    ):
        raise ValueError("label manifest hash mismatch")
    return labels


def _reject_label_reparse(path: Path) -> None:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    for ancestor in (candidate, *candidate.parents):
        try:
            stat = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("label manifest path cannot be inspected safely") from exc
        if ancestor.is_symlink() or bool(
            getattr(stat, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("label manifest path contains a symlink/junction/reparse point")


def _read_label_bytes_once(path: Path) -> bytes:
    """Read one stable regular-file handle, rejecting replacement races."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    _reject_label_reparse(candidate)
    try:
        before = candidate.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("label manifest path is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(candidate, flags), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or bool(
                getattr(opened, "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("label manifest handle is not a regular file")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("label manifest was replaced before it was read")
            content = handle.read()
        after = candidate.lstat()
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("label manifest was replaced while it was read")
        return content
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("label manifest could not be read safely") from exc


def _unique_by_case(items, kind: str):
    values = {}
    for item in items:
        if item.case_id in values:
            raise ValueError(f"duplicate {kind} case: {item.case_id}")
        values[item.case_id] = item
    return values


def _correct_vector(
    predictions: list[CasePrediction], labels: list[LabelEntry]
) -> list[bool]:
    prediction_by_id = _unique_by_case(predictions, "prediction")
    label_by_id = _unique_by_case(labels, "label")
    if set(prediction_by_id) != set(label_by_id):
        raise ValueError("prediction and label case set differs")
    values = []
    for case_id in sorted(label_by_id):
        prediction = prediction_by_id[case_id]
        candidate = prediction.candidates[0] if prediction.candidates else None
        label = label_by_id[case_id]
        values.append(
            bool(
                prediction.completed
                and candidate is not None
                and normalize_label(candidate.affected_service)
                == normalize_label(label.service)
                and normalize_fault_label(_scored_failure_class(candidate))
                == normalize_fault_label(label.fault)
            )
        )
    return values


def _billable_tokens(predictions: list[CasePrediction]) -> int:
    return sum(item.input_tokens + item.output_tokens for item in predictions)


def _validate_frozen_bundle(bundle: PredictionBundle) -> None:
    if not bundle.bundle_hash:
        raise ValueError("prediction bundle is not frozen")
    actual = canonical_json_sha256(bundle.model_dump(mode="json", exclude={"bundle_hash"}))
    if actual != bundle.bundle_hash:
        raise ValueError("prediction bundle changed after freeze")


def _validate_formal_bundle_cardinality(
    bundle: PredictionBundle, expected_case_count: int
) -> None:
    if len(bundle.predictions) != expected_case_count:
        raise ValueError("formal prediction bundle case count is not frozen")
    ids = [item.case_id for item in bundle.predictions]
    if len(ids) != len(set(ids)):
        raise ValueError("formal prediction bundle contains duplicate cases")
    if any(item.configuration != bundle.configuration for item in bundle.predictions):
        raise ValueError("formal prediction bundle contains mixed configurations")
    if any(not math.isfinite(item.duration_ms) for item in bundle.predictions):
        raise ValueError("formal prediction bundle contains non-finite metrics")


def _validate_scorer_dependency_identity(value: str | list[PredictionBundle]) -> None:
    current = scorer_dependency_hash()
    if isinstance(value, str):
        stale = value != current
    else:
        stale = any(bundle.identity.scorer_dependency_hash != current for bundle in value)
    if stale:
        raise ValueError("scorer dependency closure changed after prediction freeze")


def _validate_artifact_hash(artifact: EvaluationArtifact) -> None:
    actual = canonical_json_sha256(artifact.model_dump(mode="json", exclude={"artifact_hash"}))
    if not artifact.artifact_hash or actual != artifact.artifact_hash:
        raise ValueError("evaluation artifact changed after freeze")


def _validate_policy_hash(policy: AcceptancePolicy) -> None:
    actual = canonical_json_sha256(policy.model_dump(mode="json", exclude={"policy_hash"}))
    if not policy.policy_hash or actual != policy.policy_hash:
        raise ValueError("acceptance policy changed after freeze")


def _validate_formal_configuration_set(
    partition: str,
    bundles: dict[RcaEvalConfiguration, PredictionBundle],
) -> None:
    intended = {
        RcaEvalConfiguration.SINGLE_INTENDED,
        RcaEvalConfiguration.MULTI_INTENDED,
    }
    expected = set(RcaEvalConfiguration) if partition == "ss15" else intended
    if partition not in {"ss15", "tt90"} or set(bundles) != expected:
        raise ValueError("formal evaluation configuration set is incomplete")
    budgets = {name: bundle.budget for name, bundle in bundles.items()}
    single = budgets[RcaEvalConfiguration.SINGLE_INTENDED]
    multi = budgets[RcaEvalConfiguration.MULTI_INTENDED]
    if multi.token_budget > single.token_budget * 3:
        raise ValueError("formal Multi token budget exceeds 3x single")
    common = {
        (budget.max_turns, budget.tool_budget, budget.timeout_seconds)
        for budget in budgets.values()
    }
    if len(common) != 1:
        raise ValueError("formal evaluation contains mixed tool/turn/deadline limits")
    for configuration, budget in budgets.items():
        if (budget.max_investigators, budget.max_rounds) != EXPECTED_CONFIGURATION_TOPOLOGY[
            configuration
        ]:
            raise ValueError("formal evaluation topology limits are not frozen")
    if partition == "ss15":
        single_equal = budgets[RcaEvalConfiguration.SINGLE_EQUAL_TOKEN]
        multi_equal = budgets[RcaEvalConfiguration.MULTI_EQUAL_TOKEN]
        if single_equal.token_budget != single.token_budget * 3:
            raise ValueError("formal single equal-token budget is not 3B")
        if multi_equal.token_budget != single_equal.token_budget:
            raise ValueError("formal equal-token budgets differ")


def _nearest_rank_percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _linear_percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


if __name__ == "__main__":
    main()
