"""标签打开前的 candidate/evidence 人工审计 artifact。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from backend.benchmarks.rcaeval.models import (
    CasePrediction,
    EvidenceAuditDecision,
    EvidenceAuditExport,
    EvidenceAuditPair,
    ManualAuditArtifact,
    PredictionBundle,
)

RUBRIC = {
    "version": "rcaeval-evidence-rubric-v1",
    "checks": [
        "entity_supported",
        "temporally_compatible",
        "mechanism_relevant",
        "not_contradicted",
    ],
}


def export_evidence_pairs(
    predictions: list[CasePrediction],
    *,
    labels_visible: bool,
    prediction_bundle_hashes: dict | None = None,
) -> EvidenceAuditExport:
    if labels_visible:
        raise ValueError("evidence audit must freeze before labels are visible")
    pairs: list[EvidenceAuditPair] = []
    for prediction in sorted(predictions, key=lambda item: item.case_id):
        for rank, candidate in enumerate(prediction.candidates, start=1):
            for evidence_id in candidate.evidence_ids:
                payload = {
                    "case_id": prediction.case_id,
                    "configuration": prediction.configuration,
                    "candidate_rank": rank,
                    "evidence_id": evidence_id,
                    "affected_service": candidate.affected_service,
                    "failure_mechanism": candidate.failure_mechanism,
                }
                pairs.append(
                    EvidenceAuditPair(
                        pair_id=_canonical_hash(payload),
                        evidence_summary=prediction.evidence_summaries.get(evidence_id, ""),
                        **payload,
                    )
                )
    bindings = prediction_bundle_hashes or {}
    export_hash = _canonical_hash(
        {
            "prediction_bundle_hashes": {
                str(key): value for key, value in bindings.items()
            },
            "pairs": [item.model_dump(mode="json") for item in pairs],
        }
    )
    ids = [item.pair_id for item in pairs]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence audit export contains duplicate candidate/reference pairs")
    return EvidenceAuditExport(
        prediction_bundle_hashes=bindings,
        pairs=pairs,
        export_hash=export_hash,
    )


def freeze_manual_audit(
    export: EvidenceAuditExport,
    decisions: list[EvidenceAuditDecision],
) -> ManualAuditArtifact:
    _validate_export_hash(export)
    if not export.pairs:
        raise ValueError("manual audit cannot pass with an empty evidence export")
    ids = [item.pair_id for item in decisions]
    if len(ids) != len(set(ids)):
        raise ValueError("manual audit contains duplicate pair decisions")
    if set(ids) != {item.pair_id for item in export.pairs}:
        raise ValueError("manual audit must be complete for every exported pair")
    reviewers = {item.reviewer_id for item in decisions}
    if len(reviewers) != 1:
        raise ValueError("manual audit requires one frozen reviewer identity")
    rubric_hash = _canonical_hash(RUBRIC)
    pass_rate = None if not decisions else sum(item.passed for item in decisions) / len(decisions)
    artifact = ManualAuditArtifact(
        export_hash=export.export_hash,
        rubric_hash=rubric_hash,
        reviewer_id=next(iter(reviewers)),
        decisions=sorted(decisions, key=lambda item: item.pair_id),
        pass_rate=pass_rate,
    )
    artifact.artifact_hash = _canonical_hash(
        artifact.model_dump(mode="json", exclude={"artifact_hash"})
    )
    return artifact


def validate_manual_audit(
    export: EvidenceAuditExport,
    artifact: ManualAuditArtifact,
) -> float | None:
    """重放冻结校验，禁止 acceptance 接受裸汇总数字。"""
    _validate_export_hash(export)
    actual_artifact_hash = _canonical_hash(
        artifact.model_dump(mode="json", exclude={"artifact_hash"})
    )
    if not artifact.artifact_hash or artifact.artifact_hash != actual_artifact_hash:
        raise ValueError("manual audit changed after freeze")
    if artifact.export_hash != export.export_hash:
        raise ValueError("manual audit does not belong to the frozen export")
    if artifact.rubric_hash != _canonical_hash(RUBRIC):
        raise ValueError("manual audit rubric changed after freeze")
    pair_ids = [item.pair_id for item in artifact.decisions]
    if len(pair_ids) != len(set(pair_ids)) or set(pair_ids) != {
        item.pair_id for item in export.pairs
    }:
        raise ValueError("manual audit decisions do not match the frozen export")
    reviewers = {item.reviewer_id for item in artifact.decisions}
    if reviewers != {artifact.reviewer_id}:
        raise ValueError("manual audit reviewer identity changed after freeze")
    measured = (
        None
        if not artifact.decisions
        else sum(item.passed for item in artifact.decisions) / len(artifact.decisions)
    )
    if artifact.pass_rate != measured:
        raise ValueError("manual audit pass rate changed after freeze")
    return measured


def _validate_export_hash(export: EvidenceAuditExport) -> None:
    actual = _canonical_hash(
        {
            "prediction_bundle_hashes": {
                str(key): value
                for key, value in export.prediction_bundle_hashes.items()
            },
            "pairs": [item.model_dump(mode="json") for item in export.pairs],
        }
    )
    if export.export_hash != actual:
        raise ValueError("evidence audit export changed after freeze")


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.benchmarks.rcaeval.audit")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--bundle", type=Path, action="append", required=True)
    export.add_argument("--output", type=Path, required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--export", type=Path, required=True)
    freeze.add_argument("--decisions", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "export":
        bundles = [
            PredictionBundle.model_validate_json(path.read_text(encoding="utf-8"))
            for path in arguments.bundle
        ]
        for bundle in bundles:
            expected = _canonical_hash(
                bundle.model_dump(mode="json", exclude={"bundle_hash"})
            )
            if not bundle.bundle_hash or bundle.bundle_hash != expected:
                raise ValueError("audit refuses an unfrozen prediction bundle")
        artifact = export_evidence_pairs(
            [prediction for bundle in bundles for prediction in bundle.predictions],
            labels_visible=False,
            prediction_bundle_hashes={
                bundle.configuration: bundle.bundle_hash for bundle in bundles
            },
        )
    else:
        artifact = freeze_manual_audit(
            EvidenceAuditExport.model_validate_json(
                arguments.export.read_text(encoding="utf-8")
            ),
            [
                EvidenceAuditDecision.model_validate(item)
                for item in json.loads(arguments.decisions.read_text(encoding="utf-8"))
            ],
        )
    arguments.output.write_text(
        json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
