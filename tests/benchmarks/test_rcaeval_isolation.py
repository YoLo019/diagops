import ast
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.benchmarks.rcaeval.__main__ import _evaluate, _launch_predict, _predict
from backend.benchmarks.rcaeval.isolation import (
    build_evaluator_launch,
    build_prediction_launch,
)
from backend.benchmarks.rcaeval.models import (
    EvidenceAuditExport,
    LabelEntry,
    LabelManifest,
    ManualAuditArtifact,
    PredictionBundle,
    RcaEvalConfiguration,
    RcaEvalPartition,
    RcaEvalSystem,
    RuntimeCaseEntry,
    RuntimeManifest,
)
from backend.benchmarks.rcaeval.prepare import _canonical_sha256

OPAQUE_ID = "re2-" + "a" * 16
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _write_checksums(package_dir: Path) -> None:
    lines = []
    for path in sorted(package_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(package_dir).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {relative}")
    (package_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _runtime_manifest() -> RuntimeManifest:
    manifest = RuntimeManifest(
        upstream_repository="https://example.invalid/synthetic/rcaeval-re2",
        upstream_revision=REVISION,
        upstream_artifact="synthetic-re2",
        archive_sha256="b" * 64,
        selection_seed="synthetic-seed",
        partition_counts={
            RcaEvalPartition.OB30: 1,
            RcaEvalPartition.SS30: 0,
            RcaEvalPartition.TT90: 0,
        },
        cases=[
            RuntimeCaseEntry(
                case_id=OPAQUE_ID,
                partition=RcaEvalPartition.OB30,
                files=["telemetry-00.csv"],
            )
        ],
    )
    manifest.manifest_hash = _canonical_sha256(
        manifest.model_dump(mode="json", exclude={"manifest_hash"})
    )
    return manifest


def _label_manifest(runtime_hash: str) -> LabelManifest:
    manifest = LabelManifest(
        runtime_manifest_hash=runtime_hash,
        entries=[
            LabelEntry(
                case_id=OPAQUE_ID,
                partition=RcaEvalPartition.OB30,
                source_case_id="synthetic-source-case",
                system=RcaEvalSystem.ONLINE_BOUTIQUE,
                service="svc-a",
                fault="fault-a",
                repetition=1,
            )
        ],
    )
    manifest.manifest_hash = _canonical_sha256(
        manifest.model_dump(mode="json", exclude={"manifest_hash"})
    )
    return manifest


@pytest.fixture
def packages(tmp_path: Path) -> dict[str, Path]:
    runtime_dir = tmp_path / "packages" / "runtime"
    case_dir = runtime_dir / "cases" / OPAQUE_ID
    case_dir.mkdir(parents=True)
    (case_dir / "telemetry-00.csv").write_bytes(b"timestamp,cpu\n1784001600,0.5\n")
    manifest = _runtime_manifest()
    _write_json(runtime_dir / "manifest.json", manifest.model_dump(mode="json"))
    _write_checksums(runtime_dir)

    labels_dir = tmp_path / "packages" / "labels"
    labels_dir.mkdir()
    label_manifest = _label_manifest(manifest.manifest_hash)
    _write_json(labels_dir / "labels.json", label_manifest.model_dump(mode="json"))
    _write_checksums(labels_dir)

    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    (predictions_dir / "predictions.json").write_text(
        '{"re2-%s": {"service": "x"}}' % ("a" * 16), encoding="utf-8"
    )
    _write_checksums(predictions_dir)

    return {"runtime": runtime_dir, "labels": labels_dir, "predictions": predictions_dir}


def _bundle_hash(predictions_dir: Path) -> str:
    return hashlib.sha256((predictions_dir / "SHA256SUMS").read_bytes()).hexdigest()


def _runtime_hash() -> str:
    return _runtime_manifest().manifest_hash


def _seed_pair_ledger(predictions_root: Path, partition: str) -> None:
    from backend.benchmarks.rcaeval.__main__ import _pair_ledger_path
    from backend.benchmarks.rcaeval.ledger import CustodianPairLedger

    configurations = (
        tuple(item.value for item in RcaEvalConfiguration)
        if partition == "ss30"
        else ("single_intended", "multi_intended")
    )
    ledger = CustodianPairLedger(_pair_ledger_path(predictions_root.resolve()))
    ledger.initialize(
        partition=partition,
        prediction_set_hash="a" * 64,
        expected_sides=configurations,
    )
    for index, side in enumerate(configurations):
        side_dir = predictions_root / side
        side_dir.mkdir(parents=True, exist_ok=True)
        (side_dir / "predictions.json").write_text("{}", encoding="utf-8")
        ledger.record_side_started(side, str(side_dir))
        ledger.record_side_completed(side, f"{index + 1:064x}")
    ledger.bind_prediction_set_hash("a" * 64)


def _argv() -> list[str]:
    return ["python", "-m", "backend.benchmarks.rcaeval.runner", "--profile", "offline"]


def test_prediction_launch_happy_path(packages):
    spec = build_prediction_launch(
        runtime_package=packages["runtime"],
        predictions_dir=packages["predictions"],
        argv=_argv(),
        forbidden_locators=(str(packages["labels"]), "backend.benchmarks.rcaeval.evaluator"),
        environ={"PATH": "C:/Windows", "HOME": "should-not-leak", "SECRET": "nope"},
    )
    assert spec.argv == tuple(_argv())
    assert set(spec.env) <= {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE"}
    assert spec.env.get("PATH") == "C:/Windows"
    assert "HOME" not in spec.env and "SECRET" not in spec.env
    assert Path(spec.cwd) == packages["runtime"].resolve()


def test_prediction_launch_rejects_label_locator_in_argv(packages):
    argv = _argv() + ["--extra", str(packages["labels"])]
    with pytest.raises(ValueError, match="label/scorer locator"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=argv,
            forbidden_locators=(str(packages["labels"]),),
            environ={},
        )


def test_prediction_launch_rejects_label_locator_in_env(packages):
    with pytest.raises(ValueError, match="label/scorer locator"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            forbidden_locators=(str(packages["labels"]),),
            environ={"TEMP": str(packages["labels"])},
        )


def test_prediction_launch_rejects_label_locator_in_cwd(packages):
    with pytest.raises(ValueError, match="label/scorer locator"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            forbidden_locators=(str(packages["labels"]),),
            cwd=packages["labels"],
            environ={},
        )


def test_prediction_launch_rejects_path_traversal(packages):
    with pytest.raises(ValueError, match="traversal"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"].parent / ".." / "predictions",
            argv=_argv(),
            environ={},
        )
    with pytest.raises(ValueError, match="traversal"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            cwd=packages["runtime"] / ".." / "runtime",
            environ={},
        )


def test_prediction_launch_rejects_tampered_runtime_package(packages):
    case_file = packages["runtime"] / "cases" / OPAQUE_ID / "telemetry-00.csv"
    case_file.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            environ={},
        )


def test_prediction_launch_rejects_checksum_symlink_entry(packages, monkeypatch):
    case_file = packages["runtime"] / "cases" / OPAQUE_ID / "telemetry-00.csv"
    symlink = packages["runtime"] / "linked-telemetry.csv"
    symlink.write_bytes(case_file.read_bytes())
    _write_checksums(packages["runtime"])
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == symlink or original_is_symlink(path),
    )

    with pytest.raises(ValueError, match="symlink"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            environ={},
        )


def test_prediction_launch_rejects_tampered_manifest(packages):
    manifest_path = packages["runtime"] / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["selection_seed"] = "rewritten-seed"
    _write_json(manifest_path, payload)
    # SHA256SUMS 同步更新，只剩 manifest 内部哈希不自洽，仍必须 fail closed。
    _write_checksums(packages["runtime"])
    with pytest.raises(ValueError, match="manifest hash"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            environ={},
        )


def test_prediction_launch_rejects_provider_root_outside_package(packages, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="provider root"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            provider_roots=(outside,),
            environ={},
        )


def test_prediction_launch_rejects_predictions_inside_runtime_package(packages):
    with pytest.raises(ValueError, match="predictions"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["runtime"] / "cases",
            argv=_argv(),
            environ={},
        )


def test_prediction_launch_rejects_argv_relative_traversal(packages):
    argv = _argv() + [r"..\..\labels\labels.json"]
    with pytest.raises(ValueError, match="traversal"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=argv,
            environ={},
        )


@pytest.mark.skipif(os.name != "nt", reason="盘符大小写折叠依赖 Windows 路径语义")
def test_prediction_launch_rejects_drive_case_locator_variant(packages):
    labels = str(packages["labels"])
    locator = labels[:1].swapcase() + labels[1:]
    with pytest.raises(ValueError, match="label/scorer locator"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv() + [labels],
            forbidden_locators=(locator,),
            environ={},
        )


@pytest.mark.skipif(os.name != "nt", reason="分隔符折叠依赖 Windows 路径语义")
def test_prediction_launch_rejects_forward_slash_locator_variant(packages):
    labels = str(packages["labels"])
    locator = labels.replace("\\", "/")
    with pytest.raises(ValueError, match="label/scorer locator"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv() + [labels],
            forbidden_locators=(locator,),
            environ={},
        )


def test_prediction_launch_rejects_bare_double_dot_argv(packages):
    with pytest.raises(ValueError, match="traversal"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv() + [".."],
            environ={},
        )


def test_prediction_launch_allows_benign_double_dot_flag_value(packages):
    spec = build_prediction_launch(
        runtime_package=packages["runtime"],
        predictions_dir=packages["predictions"],
        argv=_argv() + ["--note", "wait..please"],
        environ={},
    )
    assert spec.argv[-1] == "wait..please"


@pytest.mark.parametrize(
    "helper",
    [
        r"backend\runtime\coordinator.py",
        "backend/runtime/coordinator.py",
        "backend//runtime//coordinator.py",
    ],
)
def test_evaluator_launch_rejects_slashed_production_module_paths(packages, helper):
    argv = [
        "python",
        "-m",
        "backend.benchmarks.rcaeval.evaluator",
        "--helper",
        helper,
    ]
    with pytest.raises(ValueError, match="production runtime"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash=_bundle_hash(packages["predictions"]),
            label_package=packages["labels"],
            argv=argv,
            expected_runtime_manifest_hash=_runtime_hash(),
        )


def test_evaluator_launch_rejects_unpaired_label_binding(packages):
    with pytest.raises(ValueError, match="binding"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash=_bundle_hash(packages["predictions"]),
            label_package=packages["labels"],
            argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
            expected_runtime_manifest_hash="1" * 64,
        )


def test_evaluator_launch_rejects_malformed_expected_runtime_hash(packages):
    with pytest.raises(ValueError, match="sha256"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash=_bundle_hash(packages["predictions"]),
            label_package=packages["labels"],
            argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
            expected_runtime_manifest_hash="z" * 64,
        )


def test_evaluator_launch_happy_path(packages):
    spec = build_evaluator_launch(
        predictions_bundle=packages["predictions"],
        expected_bundle_hash=_bundle_hash(packages["predictions"]),
        label_package=packages["labels"],
        argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
        expected_runtime_manifest_hash=_runtime_hash(),
    )
    assert spec.predictions_hash == _bundle_hash(packages["predictions"])
    label_manifest = _label_manifest(_runtime_manifest().manifest_hash)
    assert spec.labels_manifest_hash == label_manifest.manifest_hash
    assert spec.env == {}


def test_formal_evaluator_launch_does_not_preopen_labels(packages, monkeypatch):
    labels_path = packages["labels"] / "labels.json"
    original = Path.read_bytes

    def reject_label_read(path):
        if path == labels_path:
            raise AssertionError("formal launcher must leave the only label open to evaluator")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_label_read)
    spec = build_evaluator_launch(
        predictions_bundle=packages["predictions"],
        expected_bundle_hash=_bundle_hash(packages["predictions"]),
        label_package=packages["labels"],
        argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
        expected_runtime_manifest_hash=_runtime_hash(),
        expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
    )

    assert spec.labels_manifest_hash == _label_manifest(_runtime_hash()).manifest_hash


def test_evaluator_launch_rejects_post_freeze_change(packages):
    frozen = _bundle_hash(packages["predictions"])
    (packages["predictions"] / "predictions.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum|hash"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash=frozen,
            label_package=packages["labels"],
            argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
            expected_runtime_manifest_hash=_runtime_hash(),
        )


def test_evaluator_launch_rejects_wrong_expected_hash(packages):
    with pytest.raises(ValueError, match="hash"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash="0" * 64,
            label_package=packages["labels"],
            argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
            expected_runtime_manifest_hash=_runtime_hash(),
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "backend.diagnosis.orchestrator"],
        ["python", "-m", "backend.runtime.coordinator"],
        ["python", "-m", "backend.services.container"],
        ["python", "-m", "backend.benchmarks.openrca.evaluator"],
    ],
)
def test_evaluator_launch_rejects_production_or_foreign_entries(packages, argv):
    with pytest.raises(ValueError, match="evaluator entry"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash=_bundle_hash(packages["predictions"]),
            label_package=packages["labels"],
            argv=argv,
            expected_runtime_manifest_hash=_runtime_hash(),
        )


def test_evaluator_dependency_boundary_does_not_import_production_runtime():
    package_dir = Path(__file__).parents[2] / "backend" / "benchmarks" / "rcaeval"
    forbidden_prefixes = (
        "backend.diagnosis",
        "backend.runtime",
        "backend.rca",
        "backend.reports",
        "backend.services",
        "backend.api",
    )
    offenders: list[str] = []
    # runner 是受信 prediction 入口，必须调用生产 runtime；标签进程只允许
    # evaluator/audit/models 这一闭包，不能因同包放置而把两侧混为一谈。
    for name in ("evaluator.py", "audit.py", "models.py"):
        source_path = package_dir / name
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{source_path.name}: {name}"
                for name in names
                if name.startswith(forbidden_prefixes)
            )
    assert offenders == []


def test_prediction_runner_imports_production_v11_runtime():
    source_path = (
        Path(__file__).parents[2]
        / "backend"
        / "benchmarks"
        / "rcaeval"
        / "runner.py"
    )
    source = source_path.read_text(encoding="utf-8")

    assert "backend.diagnosis.v11_runtime" in source
    assert "backend.runtime.coordinator" in source
    assert "backend.runtime.sqlite_store" in source


def test_formal_evaluation_marks_post_child_artifact_failure_non_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.benchmarks.rcaeval import isolation

    monkeypatch.setattr(
        isolation,
        "build_evaluator_launch",
        lambda **_: SimpleNamespace(
            predictions_hash="a" * 64,
            labels_manifest_hash="b" * 64,
            argv=["evaluator"],
            cwd=tmp_path,
            env={},
        ),
    )
    monkeypatch.setattr("subprocess.run", lambda *_, **__: None)
    predictions_root = tmp_path / "predictions"
    _seed_pair_ledger(predictions_root, "tt90")
    bundles = {
        RcaEvalConfiguration.SINGLE_INTENDED: SimpleNamespace(
            configuration=RcaEvalConfiguration.SINGLE_INTENDED,
            bundle_hash="1" * 64,
        ),
        RcaEvalConfiguration.MULTI_INTENDED: SimpleNamespace(
            configuration=RcaEvalConfiguration.MULTI_INTENDED,
            bundle_hash="2" * 64,
        ),
    }
    monkeypatch.setattr(
        PredictionBundle,
        "model_validate_json",
        lambda path: bundles[RcaEvalConfiguration.MULTI_INTENDED]
        if "multi_intended" in str(path)
        else bundles[RcaEvalConfiguration.SINGLE_INTENDED],
    )
    monkeypatch.setattr(
        EvidenceAuditExport,
        "model_validate_json",
        lambda _: SimpleNamespace(export_hash="3" * 64),
    )
    monkeypatch.setattr(
        ManualAuditArtifact,
        "model_validate_json",
        lambda _: SimpleNamespace(artifact_hash="4" * 64),
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.audit.validate_prelabel_audit",
        lambda *_, **__: None,
    )
    (tmp_path / "audit.json").write_text("{}", encoding="utf-8")
    (tmp_path / "manual.json").write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "evaluation-attempt"

    with pytest.raises(FileNotFoundError):
        _evaluate(
            SimpleNamespace(
                predictions_root=predictions_root,
                partition="tt90",
                prediction_set_hash="a" * 64,
                label_package=tmp_path / "labels",
                runtime_manifest_hash="c" * 64,
                label_manifest_hash="b" * 64,
                output_dir=output_dir,
                audit_export=tmp_path / "audit.json",
                    manual_audit=tmp_path / "manual.json",
                    reauthorization_token=None,
            )
        )

    from backend.benchmarks.rcaeval.__main__ import _pair_ledger_path
    from backend.benchmarks.rcaeval.ledger import CustodianPairLedger, LedgerState

    snapshot = CustodianPairLedger(
        _pair_ledger_path(predictions_root)
    ).snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value


def test_prediction_worker_rejects_direct_unisolated_entry(monkeypatch):
    monkeypatch.delenv("RCAEVAL_PREDICTION_CHILD", raising=False)

    with pytest.raises(ValueError, match="launch-predict"):
        _predict(SimpleNamespace())


def test_prediction_launcher_sanitizes_child_and_never_passes_label_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.benchmarks.rcaeval import isolation

    captured = {}

    def build_spec(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(argv=tuple(kwargs["argv"]), cwd=str(tmp_path), env={})

    output = tmp_path / "single_intended"
    pair_root = tmp_path / "pair-root"
    output = pair_root / "single_intended"
    def run_child(*_, **__):
        output.mkdir()
        (output / "SHA256SUMS").write_text("frozen\n", encoding="utf-8")

    monkeypatch.setattr(isolation, "build_prediction_launch", build_spec)
    monkeypatch.setattr("subprocess.run", run_child)
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._prediction_pair_identity",
        lambda _: "a" * 64,
    )
    label_path = tmp_path / "private-labels"

    _launch_predict(
        SimpleNamespace(
                runtime=tmp_path / "runtime",
                pair_root=pair_root,
            partition="ss30",
            configuration="single_intended",
            base_url="https://endpoint.invalid/v1",
            capability_artifact=tmp_path / "capability.json",
            database=tmp_path / "runtime.db",
            output=output,
            token_budget=4_000,
            max_turns=8,
            tool_budget=8,
            timeout_seconds=120,
                label_package=label_path,
                reauthorization_token=None,
        )
    )

    child_argv = [str(item) for item in captured["argv"]]
    assert str(label_path) not in child_argv
    assert captured["environ"]["RCAEVAL_PREDICTION_CHILD"] == "1"
    assert str(label_path) in captured["forbidden_locators"]


def test_formal_evaluation_rejects_child_artifact_with_wrong_label_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.benchmarks.rcaeval import isolation
    from backend.benchmarks.rcaeval.models import EvaluationArtifact

    monkeypatch.setattr(
        isolation,
        "build_evaluator_launch",
        lambda **_: SimpleNamespace(
            predictions_hash="a" * 64,
            labels_manifest_hash="b" * 64,
            argv=["evaluator"],
            cwd=tmp_path,
            env={},
        ),
    )
    output_dir = tmp_path / "evaluation-attempt"
    predictions_root = tmp_path / "predictions"
    _seed_pair_ledger(predictions_root, "tt90")
    bundles = {
        RcaEvalConfiguration.SINGLE_INTENDED: SimpleNamespace(
            configuration=RcaEvalConfiguration.SINGLE_INTENDED,
            bundle_hash="1" * 64,
        ),
        RcaEvalConfiguration.MULTI_INTENDED: SimpleNamespace(
            configuration=RcaEvalConfiguration.MULTI_INTENDED,
            bundle_hash="2" * 64,
        ),
    }
    monkeypatch.setattr(
        PredictionBundle,
        "model_validate_json",
        lambda path: bundles[RcaEvalConfiguration.MULTI_INTENDED]
        if "multi_intended" in str(path)
        else bundles[RcaEvalConfiguration.SINGLE_INTENDED],
    )
    monkeypatch.setattr(
        EvidenceAuditExport,
        "model_validate_json",
        lambda _: SimpleNamespace(export_hash="3" * 64),
    )
    monkeypatch.setattr(
        ManualAuditArtifact,
        "model_validate_json",
        lambda _: SimpleNamespace(artifact_hash="4" * 64),
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.audit.validate_prelabel_audit",
        lambda *_, **__: None,
    )
    (tmp_path / "audit.json").write_text("{}", encoding="utf-8")
    (tmp_path / "manual.json").write_text("{}", encoding="utf-8")

    def run_child(*_, **__):
        (output_dir / "evaluation.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("subprocess.run", run_child)
    monkeypatch.setattr(
        EvaluationArtifact,
        "model_validate_json",
        lambda _: SimpleNamespace(
            label_open_count=1,
            labels_manifest_hash="d" * 64,
            runtime_manifest_hash="c" * 64,
            partition=RcaEvalPartition.TT90,
            artifact_hash="e" * 64,
        ),
    )

    with pytest.raises(ValueError, match="label.*binding"):
        _evaluate(
            SimpleNamespace(
                predictions_root=predictions_root,
                partition="tt90",
                prediction_set_hash="a" * 64,
                label_package=tmp_path / "labels",
                runtime_manifest_hash="c" * 64,
                label_manifest_hash="b" * 64,
                output_dir=output_dir,
                audit_export=tmp_path / "audit.json",
                    manual_audit=tmp_path / "manual.json",
                    reauthorization_token=None,
            )
        )

    from backend.benchmarks.rcaeval.__main__ import _pair_ledger_path
    from backend.benchmarks.rcaeval.ledger import CustodianPairLedger, LedgerState

    snapshot = CustodianPairLedger(
        _pair_ledger_path(predictions_root)
    ).snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value
