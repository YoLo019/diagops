"""RCAEval 准备工具 CLI：inspect 只读预览，prepare 产出隔离的 runtime/标签包。

数据集与标签永远位于仓库之外；本命令只接受显式 --source 与 --pin。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from backend.benchmarks.rcaeval.prepare import (
    inspect_source,
    load_pin,
    prepare_partitions,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.benchmarks.rcaeval")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="校验源与 pin 并预览分区选择，不写文件")
    inspect.add_argument("--source", type=Path, required=True)
    inspect.add_argument("--pin", type=Path, required=True)

    prepare = commands.add_parser("prepare", help="产出隔离的 runtime 包与 evaluator-only 标签包")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--pin", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    launch_predict = commands.add_parser(
        "launch-predict", help="隔离启动真实 V11 prediction 子进程并归档尝试"
    )
    _add_prediction_arguments(launch_predict)
    launch_predict.add_argument(
        "--pair-root",
        type=Path,
        required=True,
        help="output grouping root; it must be inside the frozen custodian root",
    )
    launch_predict.add_argument(
        "--custodian-manifest",
        type=Path,
        required=True,
        help="immutable custodian-owned root manifest produced by prepare",
    )
    launch_predict.add_argument("--reauthorization-token")
    launch_predict.add_argument(
        "--label-package",
        type=Path,
        required=True,
        help="仅供可信父启动器排除；绝不传给 prediction 子进程",
    )
    predict = commands.add_parser(
        "predict", help="受控启动器内部使用的 prediction worker"
    )
    _add_prediction_arguments(predict)

    freeze_set = commands.add_parser(
        "freeze-set", help="在所有 prediction 配置完成后冻结 evaluator 输入根"
    )
    freeze_set.add_argument("--root", type=Path, required=True)
    freeze_set.add_argument("--custodian-manifest", type=Path, required=True)
    freeze_set.add_argument("--partition", choices=("ss30", "tt90"), required=True)

    evaluate = commands.add_parser(
        "evaluate", help="在隔离子进程中只打开一次 labels 并评估冻结 prediction set"
    )
    evaluate.add_argument("--predictions-root", type=Path, required=True)
    evaluate.add_argument("--custodian-manifest", type=Path, required=True)
    evaluate.add_argument("--partition", choices=("ss30", "tt90"), required=True)
    evaluate.add_argument("--prediction-set-hash", required=True)
    evaluate.add_argument("--label-package", type=Path, required=True)
    evaluate.add_argument("--runtime-manifest-hash", required=True)
    evaluate.add_argument("--label-manifest-hash", required=True)
    evaluate.add_argument("--audit-export", type=Path, required=True)
    evaluate.add_argument("--manual-audit", type=Path, required=True)
    evaluate.add_argument("--reauthorization-token")
    evaluate.add_argument("--output-dir", type=Path, required=True)

    freeze_policy = commands.add_parser(
        "freeze-policy", help="从冻结 SS30 evaluation 派生并冻结 TT90 acceptance policy"
    )
    freeze_policy.add_argument("--sealed-validation", type=Path, required=True)
    freeze_policy.add_argument("--tt90-manifest-hash", required=True)
    freeze_policy.add_argument("--output", type=Path, required=True)

    accept = commands.add_parser(
        "accept", help="按冻结 policy 和人工 evidence audit 归档 TT90 gate 结果"
    )
    accept.add_argument("--evaluation", type=Path, required=True)
    accept.add_argument("--policy", type=Path, required=True)
    accept.add_argument("--bundle", type=Path, action="append", required=True)
    accept.add_argument("--audit-export", type=Path, required=True)
    accept.add_argument("--manual-audit", type=Path, required=True)
    accept.add_argument("--output", type=Path, required=True)

    arguments = parser.parse_args()
    if arguments.command == "inspect":
        pin = load_pin(arguments.pin)
        report = inspect_source(arguments.source, pin)
        summary = {
            "source_case_count": report.source_case_count,
            "partition_counts": {
                partition.value: count for partition, count in report.partition_counts.items()
            },
            "selected_source_ids": {
                partition.value: ids for partition, ids in report.selected_source_ids.items()
            },
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if arguments.command == "prepare":
        result = prepare_partitions(arguments.source, arguments.pin, arguments.output)
        print(result.runtime_manifest.manifest_hash)
        return
    if arguments.command == "launch-predict":
        _launch_predict(arguments)
        return
    if arguments.command == "predict":
        _predict(arguments)
        return
    if arguments.command == "freeze-set":
        _freeze_set(arguments)
        return
    if arguments.command == "evaluate":
        _evaluate(arguments)
        return
    if arguments.command == "freeze-policy":
        _freeze_policy(arguments)
        return
    _accept(arguments)


def _add_prediction_arguments(parser) -> None:
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--partition", choices=("ob30", "ss30", "tt90"), required=True)
    parser.add_argument(
        "--configuration",
        choices=(
            "single_intended",
            "multi_intended",
            "single_equal_token",
            "multi_equal_token",
        ),
        required=True,
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--capability-artifact", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-budget", type=int, required=True)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--tool-budget", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=120)


def _launch_predict(arguments) -> None:
    from backend.benchmarks.rcaeval.isolation import (
        build_prediction_launch,
        verify_runtime_package,
    )
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        canonical_creation_locator,
    )
    from backend.services.source_identity import reject_reparse_path

    reject_reparse_path(arguments.output, "prediction output")
    reject_reparse_path(arguments.pair_root, "prediction pair root")
    # 两个调用方 root 都会成为持久化身份输入；先拒绝相对/别名拼写，
    # 再禁止按进程 cwd 解析。输出此时允许尚未创建，绑定最近存在祖先。
    canonical_creation_locator(arguments.output)
    canonical_creation_locator(arguments.pair_root)
    output = arguments.output.resolve()
    pair_root = arguments.pair_root.resolve()
    if not _within(output, pair_root):
        raise ValueError("prediction output must stay inside the custodian pair root")
    if output.exists():
        raise ValueError("prediction output already exists")
    ledger = CustodianPairLedger.from_manifest(arguments.custodian_manifest)
    canonical_root = ledger.canonical_root
    if not _same_path(arguments.runtime, canonical_root / "runtime"):
        raise ValueError("runtime package is not the custodian-frozen runtime root")
    if verify_runtime_package(arguments.runtime).manifest_hash != ledger.runtime_manifest_hash:
        raise ValueError("runtime manifest differs from the custodian root manifest")
    if not _same_path(arguments.label_package, canonical_root / "labels"):
        raise ValueError("label package is not the custodian-frozen label root")
    if not _within(pair_root, canonical_root):
        raise ValueError("pair root must stay inside the canonical custodian root")
    pair_root.mkdir(parents=True, exist_ok=True)
    pair_identity = _prediction_pair_identity(arguments)
    sides = _formal_configuration_names(arguments.partition)
    if arguments.reauthorization_token:
        ledger.reauthorize(
            arguments.reauthorization_token,
            authorized_evaluation_identity=pair_identity,
        )
    ledger.initialize(
        partition=arguments.partition,
        prediction_set_hash=pair_identity,
        expected_sides=sides,
    )
    prediction_lease = ledger.record_side_started(arguments.configuration, str(output))
    try:
        ledger.heartbeat(prediction_lease)
        output.parent.mkdir(parents=True, exist_ok=True)
        child_argv = [
            sys.executable,
            str(Path(__file__).with_name("prediction_worker.py").resolve()),
            "--source-root",
            str(_repository_root()),
            "--runtime",
            str(arguments.runtime),
            "--partition",
            arguments.partition,
            "--configuration",
            arguments.configuration,
            "--base-url",
            arguments.base_url,
            "--capability-artifact",
            str(arguments.capability_artifact),
            "--database",
            str(arguments.database),
            "--output",
            str(output),
            "--token-budget",
            str(arguments.token_budget),
            "--max-turns",
            str(arguments.max_turns),
            "--tool-budget",
            str(arguments.tool_budget),
            "--timeout-seconds",
            str(arguments.timeout_seconds),
        ]
        child_environ = dict(os.environ)
        child_environ["RCAEVAL_PREDICTION_CHILD"] = "1"
        evaluator_path = Path(__file__).with_name("evaluator.py").resolve()
        spec = build_prediction_launch(
            runtime_package=arguments.runtime,
            predictions_dir=output,
            argv=child_argv,
            forbidden_locators=(
                str(arguments.label_package.resolve()),
                str(evaluator_path),
            ),
            cwd=arguments.runtime,
            environ=child_environ,
        )
        heartbeat_seconds = max(1.0, float(ledger.snapshot()["lease_seconds"]) / 3.0)
        process = subprocess.Popen(spec.argv, cwd=spec.cwd, env=spec.env)
        try:
            while True:
                try:
                    process.wait(timeout=heartbeat_seconds)
                    break
                except subprocess.TimeoutExpired:
                    # 正式 30/90 例运行远超默认 900s lease；子进程存活期间必须
                    # 持续续租，否则完成时 lease 过期会被判为不可恢复失败。
                    ledger.heartbeat(prediction_lease)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)
        sums_path = output / "SHA256SUMS"
        bundle_hash = hashlib.sha256(sums_path.read_bytes()).hexdigest()
        ledger.record_side_completed(
            arguments.configuration,
            bundle_hash,
            lease_token=prediction_lease,
        )
    except BaseException:
        ledger.invalidate_pair("prediction side failed or was interrupted")
        raise
    print(f"prediction completed: {bundle_hash}")


def _predict(arguments) -> None:
    if os.environ.get("RCAEVAL_PREDICTION_CHILD") != "1":
        raise ValueError("predict worker must be invoked through launch-predict")
    # 重依赖仅属于可信 prediction 进程；evaluator 模块不会导入这条闭包。
    from backend.benchmarks.rcaeval.isolation import verify_runtime_package
    from backend.benchmarks.rcaeval.models import (
        EXPECTED_PARTITION_COUNTS,
        EndpointCapabilityIdentity,
        EvaluationBudget,
        PredictionBundle,
        RcaEvalConfiguration,
        RcaEvalPartition,
    )
    from backend.benchmarks.rcaeval.runner import (
        RcaEvalCaseRunner,
        freeze_prediction_bundle,
        frozen_run_identity,
    )
    from backend.config.settings import canonicalize_endpoint, endpoint_id
    from backend.db.session import create_db_engine, initialize_database
    from backend.db.sqlite_repository import SQLiteInvestigationRepository
    from backend.diagnosis.openai_compatible_model import (
        create_openai_compatible_model,
    )
    from backend.runtime.sqlite_store import SQLiteRuntimeStore
    from backend.services.model_capability import (
        read_capability_artifact,
        validate_capability_for_prediction,
    )

    manifest = verify_runtime_package(arguments.runtime)
    artifact = read_capability_artifact(arguments.capability_artifact)
    canonical_endpoint = canonicalize_endpoint(arguments.base_url)
    validate_capability_for_prediction(
        artifact,
        provider="openai_compatible",
        model=artifact.model,
        endpoint_id_value=endpoint_id(canonical_endpoint),
        expected_parallelism=3,
        repository_root=_repository_root(),
    )
    api_key = os.environ.get("DIAGOPS_AGENTS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("DIAGOPS_AGENTS_API_KEY is required for prediction")
    model = create_openai_compatible_model(
        artifact.model,
        api_key,
        canonical_endpoint,
        timeout_seconds=arguments.timeout_seconds,
        max_retries=0,
        structured_output_transport=artifact.structured_output_transport,
    )
    if model is None:
        raise ValueError("prediction model adapter is unavailable")
    capability = EndpointCapabilityIdentity(
        provider=artifact.provider,
        model=artifact.model,
        api_mode=artifact.api_mode,
        structured_output_transport=artifact.structured_output_transport,
        endpoint_id=artifact.endpoint_id,
        artifact_hash=artifact.artifact_hash,
    )
    configuration = RcaEvalConfiguration(arguments.configuration)
    budget = EvaluationBudget(
        configuration=configuration,
        token_budget=arguments.token_budget,
        max_turns=arguments.max_turns,
        tool_budget=arguments.tool_budget,
        timeout_seconds=arguments.timeout_seconds,
        max_investigators=3 if configuration.is_multi else 1,
        max_rounds=2 if configuration.is_multi else 1,
    )
    arguments.database.parent.mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(f"sqlite:///{arguments.database.resolve()}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    runtime_store = SQLiteRuntimeStore(engine, repository)
    runner = RcaEvalCaseRunner(
        runtime_package=arguments.runtime,
        model=model,
        capability=capability,
        repository=repository,
        runtime_store=runtime_store,
    )
    partition = RcaEvalPartition(arguments.partition)
    cases = [item for item in manifest.cases if item.partition == partition]
    if len(cases) != EXPECTED_PARTITION_COUNTS[partition]:
        raise ValueError("prediction partition case count is not frozen")
    predictions = [runner.run_case(case, budget) for case in cases]
    contracts = {
        prediction.execution_contract_hash: runtime_store.get_run(
            prediction.runtime_run_id
        ).execution_contract
        for prediction in predictions
    }
    if len(contracts) != 1:
        raise ValueError("prediction run contracts mixed within one configuration")
    contract = next(iter(contracts.values()))
    identity = frozen_run_identity(
        runtime_manifest_hash=manifest.manifest_hash,
        capability=capability,
        tool_manifest_hash_value=contract["tool_manifest_hash"],
        skill_catalog_hash_value=contract["skill_catalog"]["catalog_hash"],
        repository_root=_repository_root(),
    )
    bundle = PredictionBundle(
        partition=partition,
        configuration=configuration,
        identity=identity,
        budget=budget,
        predictions=predictions,
        frozen_at=datetime.now(UTC),
    )
    bundle_hash = freeze_prediction_bundle(bundle, arguments.output)
    print(f"prediction bundle frozen: {bundle_hash}")


def _freeze_set(arguments) -> None:
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        canonical_creation_locator,
    )
    from backend.benchmarks.rcaeval.models import RcaEvalConfiguration
    from backend.benchmarks.rcaeval.runner import freeze_prediction_set
    from backend.services.source_identity import reject_reparse_path

    if arguments.partition == "ss30":
        configurations = set(RcaEvalConfiguration)
        count = 30
    else:
        configurations = {
            RcaEvalConfiguration.SINGLE_INTENDED,
            RcaEvalConfiguration.MULTI_INTENDED,
        }
        count = 90
    ledger = CustodianPairLedger.from_manifest(arguments.custodian_manifest)
    reject_reparse_path(arguments.root, "prediction set root")
    canonical_creation_locator(arguments.root)
    root = arguments.root.resolve()
    if not _within(root, ledger.canonical_root):
        raise ValueError("prediction root must stay inside the canonical custodian root")
    digest = freeze_prediction_set(
        root,
        expected_configurations=configurations,
        expected_case_count=count,
        ledger=ledger,
    )
    ledger.bind_prediction_set_hash(digest)
    print(f"prediction set frozen: {digest}")


def _evaluate(arguments) -> None:
    from backend.benchmarks.rcaeval.audit import validate_prelabel_audit
    from backend.benchmarks.rcaeval.isolation import (
        EvaluatorLaunchSpec,
        build_evaluator_launch,
    )
    from backend.benchmarks.rcaeval.ledger import CustodianPairLedger
    from backend.benchmarks.rcaeval.models import (
        EvaluationArtifact,
        EvidenceAuditExport,
        ManualAuditArtifact,
        PredictionBundle,
        RcaEvalConfiguration,
    )

    configurations = (
        list(RcaEvalConfiguration)
        if arguments.partition == "ss30"
        else [
            RcaEvalConfiguration.SINGLE_INTENDED,
            RcaEvalConfiguration.MULTI_INTENDED,
        ]
    )
    output_dir = arguments.output_dir.resolve()
    prediction_root = arguments.predictions_root.resolve()
    if output_dir.exists():
        raise ValueError("evaluation output already exists; labels will not be reopened")
    if _within(output_dir, prediction_root):
        raise ValueError("evaluation output must stay outside the frozen prediction root")
    evaluation_path = output_dir / "evaluation.json"
    ledger = CustodianPairLedger.from_manifest(arguments.custodian_manifest)
    if not _same_path(prediction_root.parent, ledger.canonical_root):
        raise ValueError("prediction root is not mapped to the canonical custodian root")
    if not _same_path(arguments.label_package, ledger.canonical_root / "labels"):
        raise ValueError("label package is not the custodian-frozen label root")
    if arguments.runtime_manifest_hash != ledger.runtime_manifest_hash:
        raise ValueError("runtime manifest differs from the custodian root manifest")
    if arguments.label_manifest_hash != ledger.label_manifest_hash:
        raise ValueError("label manifest differs from the custodian root manifest")
    argv = [sys.executable, "-m", "backend.benchmarks.rcaeval.evaluator"]
    for configuration in configurations:
        argv.extend(
            [
                "--bundle",
                str(
                    arguments.predictions_root
                    / configuration.value
                    / "predictions.json"
                ),
            ]
        )
    argv.extend(
        [
            "--labels",
            str(arguments.label_package / "labels.json"),
            "--output",
            str(evaluation_path),
            "--ledger",
            str(ledger.path),
            "--custodian-manifest",
            str(arguments.custodian_manifest),
            "--partition",
            arguments.partition,
            "--prediction-set-hash",
            arguments.prediction_set_hash,
            "--audit-export",
            str(arguments.audit_export),
            "--manual-audit",
            str(arguments.manual_audit),
            "--expected-label-manifest-hash",
            arguments.label_manifest_hash,
            "--expected-runtime-manifest-hash",
            arguments.runtime_manifest_hash,
        ]
    )
    spec = build_evaluator_launch(
        predictions_bundle=arguments.predictions_root,
        expected_bundle_hash=arguments.prediction_set_hash,
        label_package=arguments.label_package,
        argv=argv,
        expected_runtime_manifest_hash=arguments.runtime_manifest_hash,
        expected_label_manifest_hash=arguments.label_manifest_hash,
        cwd=Path(__file__).resolve().parents[3],
    )
    bundles = {
        configuration: PredictionBundle.model_validate_json(
            (prediction_root / configuration.value / "predictions.json").read_text(
                encoding="utf-8"
            )
        )
        for configuration in configurations
    }
    expected_bundle_hashes = {
        configuration: bundle.bundle_hash
        for configuration, bundle in bundles.items()
    }
    audit_export = EvidenceAuditExport.model_validate_json(
        arguments.audit_export.read_text(encoding="utf-8")
    )
    manual_audit = ManualAuditArtifact.model_validate_json(
        arguments.manual_audit.read_text(encoding="utf-8")
    )
    if arguments.reauthorization_token:
        ledger.reauthorize(
            arguments.reauthorization_token,
            authorized_evaluation_identity=arguments.prediction_set_hash,
        )
    snapshot = ledger.snapshot()
    ledger.initialize(
        partition=arguments.partition,
        prediction_set_hash=str(snapshot["prediction_set_hash"]),
        expected_sides=tuple(configuration.value for configuration in configurations),
    )
    validate_prelabel_audit(
        audit_export,
        manual_audit,
        expected_bundle_hashes=expected_bundle_hashes,
        frozen_bundles=bundles,
    )
    ledger.bind_prediction_set_hash(spec.predictions_hash)
    evaluation_reserved = False
    try:
        reservation = ledger.reserve_evaluation(
            audit_export_hash=audit_export.export_hash,
            manual_audit_hash=manual_audit.artifact_hash,
        )
        evaluation_reserved = True
        label_open = ledger.reserve_label_open(
            audit_export_hash=audit_export.export_hash,
            manual_audit_hash=manual_audit.artifact_hash,
            reservation_token=reservation.reservation_token,
        )
        ledger.heartbeat(label_open.lease_token)
        spec = EvaluatorLaunchSpec(
            argv=(*spec.argv, "--label-open-token", label_open.lease_token),
            env=spec.env,
            cwd=spec.cwd,
            predictions_hash=spec.predictions_hash,
            labels_manifest_hash=spec.labels_manifest_hash,
        )
        subprocess.run(spec.argv, cwd=spec.cwd, env=spec.env, check=True)
        artifact = EvaluationArtifact.model_validate_json(
            evaluation_path.read_text(encoding="utf-8")
        )
        if artifact.label_open_count != 1:
            raise ValueError("formal evaluator did not preserve single-open label contract")
        if artifact.partition.value != arguments.partition:
            raise ValueError("formal evaluator returned the wrong partition")
        if artifact.runtime_manifest_hash != arguments.runtime_manifest_hash:
            raise ValueError("formal evaluator returned the wrong runtime binding")
        if artifact.labels_manifest_hash != spec.labels_manifest_hash:
            raise ValueError("formal evaluator returned the wrong label binding")
        ledger.mark_evaluation_completed(
            artifact.artifact_hash,
            lease_token=label_open.lease_token,
        )
    except BaseException:
        if evaluation_reserved:
            ledger.invalidate_pair("label-side evaluation failed or was interrupted")
        raise
    print(f"evaluation completed: {artifact.artifact_hash}")


def _freeze_policy(arguments) -> None:
    from backend.benchmarks.rcaeval.evaluator import freeze_acceptance_policy
    from backend.benchmarks.rcaeval.models import EvaluationArtifact

    sealed_validation = EvaluationArtifact.model_validate_json(
        arguments.sealed_validation.read_text(encoding="utf-8")
    )
    policy = freeze_acceptance_policy(
        sealed_validation=sealed_validation,
        tt90_manifest_hash=arguments.tt90_manifest_hash,
    )
    _write_new_artifact(arguments.output, policy)
    print(f"acceptance policy frozen: {policy.policy_hash}")


def _accept(arguments) -> None:
    from backend.benchmarks.rcaeval.evaluator import freeze_acceptance_result
    from backend.benchmarks.rcaeval.models import (
        AcceptancePolicy,
        EvaluationArtifact,
        EvidenceAuditExport,
        ManualAuditArtifact,
        PredictionBundle,
    )

    result = freeze_acceptance_result(
        EvaluationArtifact.model_validate_json(
            arguments.evaluation.read_text(encoding="utf-8")
        ),
        AcceptancePolicy.model_validate_json(
            arguments.policy.read_text(encoding="utf-8")
        ),
        audit_export=EvidenceAuditExport.model_validate_json(
            arguments.audit_export.read_text(encoding="utf-8")
        ),
        manual_audit=ManualAuditArtifact.model_validate_json(
            arguments.manual_audit.read_text(encoding="utf-8")
        ),
        frozen_bundles=[
            PredictionBundle.model_validate_json(path.read_text(encoding="utf-8"))
            for path in arguments.bundle
        ],
    )
    _write_new_artifact(arguments.output, result)
    print(f"TT90 acceptance archived: {result.artifact_hash}")


def _write_new_artifact(path: Path, artifact) -> None:
    if path.exists():
        raise ValueError("frozen artifact output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _formal_configuration_names(partition: str) -> tuple[str, ...]:
    from backend.benchmarks.rcaeval.models import RcaEvalConfiguration

    if partition == "ss30":
        return tuple(item.value for item in RcaEvalConfiguration)
    if partition == "tt90":
        return ("single_intended", "multi_intended")
    return ("single_intended",)


def _prediction_pair_identity(arguments) -> str:
    from backend.benchmarks.rcaeval.isolation import verify_runtime_package
    from backend.services.model_capability import read_capability_artifact
    from backend.services.source_identity import resolve_source_identity

    manifest = verify_runtime_package(arguments.runtime)
    capability = read_capability_artifact(arguments.capability_artifact)
    source_identity = resolve_source_identity(_repository_root())
    payload = {
        "partition": arguments.partition,
        "runtime_manifest_hash": manifest.manifest_hash,
        "capability_artifact_hash": capability.artifact_hash,
        "source_revision": source_identity.revision,
        "source_manifest_hash": source_identity.manifest_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    main()
