import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.benchmarks.rcaeval.isolation import (
    build_evaluator_launch,
    build_prediction_launch,
)
from backend.benchmarks.rcaeval.models import (
    LabelEntry,
    LabelManifest,
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


def test_rcaeval_package_does_not_import_production_runtime():
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
    for source_path in package_dir.glob("*.py"):
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
