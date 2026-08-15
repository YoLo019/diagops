import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.benchmarks.rcaeval.__main__ import _evaluate, _launch_predict, _predict
from backend.benchmarks.rcaeval.isolation import (
    build_evaluator_launch,
    build_prediction_launch,
)
from backend.benchmarks.rcaeval.models import (
    CandidatePrediction,
    CasePrediction,
    EndpointCapabilityIdentity,
    EvaluationBudget,
    EvidenceAuditExport,
    FrozenRunIdentity,
    LabelEntry,
    LabelManifest,
    ManualAuditArtifact,
    PredictionBundle,
    RcaEvalConfiguration,
    RcaEvalPartition,
    RcaEvalSystem,
    RuntimeCaseEntry,
    RuntimeManifest,
    canonical_json_sha256,
)
from backend.benchmarks.rcaeval.runner import (
    freeze_prediction_bundle,
    freeze_prediction_set,
)

OPAQUE_ID = "re2-" + "a" * 16
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _write_checksums(package_dir: Path) -> None:
    entries: list[tuple[str, str]] = []
    for path in package_dir.rglob("*"):
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(package_dir).as_posix()
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = [f"{digest}  {relative}" for relative, digest in sorted(entries)]
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
    manifest.manifest_hash = canonical_json_sha256(
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
    manifest.manifest_hash = canonical_json_sha256(
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
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        create_custodian_manifest,
    )

    configurations = (
        tuple(item.value for item in RcaEvalConfiguration)
        if partition == "ss30"
        else ("single_intended", "multi_intended")
    )
    manifest = create_custodian_manifest(
        predictions_root.parent,
        runtime_manifest_hash="c" * 64,
        label_manifest_hash="b" * 64,
    )
    ledger = CustodianPairLedger.from_manifest(manifest)
    ledger.initialize(
        partition=partition,
        prediction_set_hash="a" * 64,
        expected_sides=configurations,
    )
    for index, side in enumerate(configurations):
        side_dir = predictions_root / side
        side_dir.mkdir(parents=True, exist_ok=True)
        (side_dir / "predictions.json").write_text("{}", encoding="utf-8")
        lease = ledger.record_side_started(side, str(side_dir))
        ledger.record_side_completed(side, f"{index + 1:064x}", lease_token=lease)
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash="a" * 64,
        root_locator=str(ledger.canonical_root / "predictions"),
    )
    ledger.bind_prediction_set_hash("a" * 64)


def _tiny_frozen_bundle(configuration: RcaEvalConfiguration) -> PredictionBundle:
    identity = FrozenRunIdentity(
        source_commit="1" * 40,
        source_manifest_hash="e" * 64,
        runtime_manifest_hash="2" * 64,
        capability=EndpointCapabilityIdentity(
            provider="openai_compatible",
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
        scorer_dependency_hash="9" * 64,
        dependency_lock_hash="a" * 64,
        memory_snapshot_hash="b" * 64,
        retry_policy_hash="c" * 64,
    )
    budget = EvaluationBudget(
        configuration=configuration,
        token_budget=4_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
        max_investigators=3 if configuration.is_multi else 1,
        max_rounds=2 if configuration.is_multi else 1,
    )
    prediction = CasePrediction(
        case_id=OPAQUE_ID,
        configuration=configuration,
        completed=True,
        candidates=[
            CandidatePrediction(
                affected_service="service",
                failure_mechanism="fault",
                evidence_ids=[],
            )
        ],
        runtime_run_id=f"run-{configuration.value}",
        execution_contract_hash="d" * 64,
    )
    return PredictionBundle(
        partition=RcaEvalPartition.TT90,
        configuration=configuration,
        identity=identity,
        budget=budget,
        predictions=[prediction],
        frozen_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


def test_freeze_prediction_bundle_rejects_incomplete_prediction(tmp_path):
    bundle = _tiny_frozen_bundle(RcaEvalConfiguration.SINGLE_INTENDED)
    failed = bundle.predictions[0].model_copy(
        update={"completed": False, "failure_category": "ValueError"}
    )
    bundle = bundle.model_copy(update={"predictions": [failed]})
    output = tmp_path / "failed-side"

    with pytest.raises(ValueError, match="incomplete prediction"):
        freeze_prediction_bundle(bundle, output)

    assert not output.exists()


def test_freeze_set_requires_ledger_side_output_binding(tmp_path):
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        create_custodian_manifest,
    )

    custodian_root = tmp_path / "custodian"
    predictions_root = custodian_root / "predictions"
    manifest = create_custodian_manifest(
        custodian_root,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    ledger = CustodianPairLedger.from_manifest(manifest)
    configurations = {
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
                "source_manifest_hash": "e" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    ledger.initialize(
        partition="tt90",
        prediction_set_hash=pair_identity,
        expected_sides=tuple(item.value for item in configurations),
    )
    for configuration in sorted(configurations, key=lambda item: item.value):
        side_dir = predictions_root / configuration.value
        actual_side_hash = freeze_prediction_bundle(
            _tiny_frozen_bundle(configuration),
            side_dir,
        )
        lease = ledger.record_side_started(configuration.value, str(side_dir))
        del actual_side_hash
        ledger.record_side_completed(
            configuration.value,
            "f" * 64,
            lease_token=lease,
        )

    with pytest.raises(ValueError, match="ledger|bundle|side"):
        freeze_prediction_set(
            predictions_root,
            expected_configurations=configurations,
            expected_case_count=1,
            ledger=ledger,
        )
    assert not (predictions_root / "SHA256SUMS").exists()


def test_freeze_set_rejects_outside_root_before_first_write(tmp_path, monkeypatch):
    from backend.benchmarks.rcaeval import __main__ as cli
    from backend.benchmarks.rcaeval.ledger import create_custodian_manifest

    custodian_root = tmp_path / "custodian"
    manifest = create_custodian_manifest(
        custodian_root,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    outside_root = tmp_path / "outside" / "predictions"
    marker = tmp_path / "wrote-before-reject"

    def fake_freeze(*args, **kwargs):
        marker.write_text("unexpected write", encoding="utf-8")
        return "a" * 64

    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.runner.freeze_prediction_set",
        fake_freeze,
    )
    with pytest.raises(ValueError, match="canonical custodian root"):
        cli._freeze_set(
            SimpleNamespace(
                root=outside_root,
                custodian_manifest=manifest,
                partition="tt90",
            )
        )
    assert not marker.exists()


def test_frozen_run_identity_accepts_packaged_source_without_git_and_rejects_tamper(
    tmp_path,
):
    from backend.benchmarks.rcaeval.runner import frozen_run_identity
    from backend.services.source_identity import write_source_manifest

    package_root = tmp_path / "package"
    (package_root / "backend" / "diagnosis").mkdir(parents=True)
    (package_root / "backend" / "diagnosis" / "v11_runtime.py").write_text(
        "PACKAGE_RUNTIME = True\n",
        encoding="utf-8",
    )
    (package_root / "pyproject.toml").write_text("[project]\nname='package'\n", encoding="utf-8")
    (package_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    write_source_manifest(package_root)

    capability = EndpointCapabilityIdentity(
        provider="openai_compatible",
        model="packaged-model",
        api_mode="chat_completions",
        endpoint_id="endpoint",
        artifact_hash="a" * 64,
    )
    identity = frozen_run_identity(
        runtime_manifest_hash="b" * 64,
        capability=capability,
        tool_manifest_hash_value="c" * 64,
        skill_catalog_hash_value="d" * 64,
        repository_root=package_root,
    )
    assert len(identity.source_commit) == 40
    assert len(identity.source_manifest_hash) == 64

    (package_root / "backend" / "diagnosis" / "v11_runtime.py").write_text(
        "PACKAGE_RUNTIME = False\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest|digest|source"):
        frozen_run_identity(
            runtime_manifest_hash="b" * 64,
            capability=capability,
            tool_manifest_hash_value="c" * 64,
            skill_catalog_hash_value="d" * 64,
            repository_root=package_root,
        )


def test_packaged_source_manifest_survives_relocation_without_git(tmp_path):
    from backend.services.source_identity import resolve_source_identity, write_source_manifest

    package_root = tmp_path / "site-packages"
    worker = package_root / "backend" / "benchmarks" / "rcaeval" / "prediction_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("PACKAGE_WORKER = True\n", encoding="utf-8")
    manifest = package_root / "backend" / "services" / "diagops-source-manifest.json"
    write_source_manifest(package_root, output=manifest, package_scope=True)

    identity = resolve_source_identity(package_root)
    assert identity.git_dirty is False
    assert len(identity.manifest_hash) == 64

    worker.write_text("PACKAGE_WORKER = False\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest|digest|source"):
        resolve_source_identity(package_root)


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


@pytest.mark.skipif(os.name != "nt", reason="Windows junction attributes are platform-specific")
def test_prediction_launch_rejects_real_windows_junction(packages, tmp_path):
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "escaped.txt").write_text("escaped", encoding="utf-8")
    junction = packages["runtime"] / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("junction creation unavailable in this Windows environment")
    with pytest.raises(ValueError, match="reparse|junction|symlink|checksum"):
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
            expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
        )


def test_evaluator_launch_rejects_unpaired_label_binding(packages):
    from backend.benchmarks.rcaeval.evaluator import read_label_manifest_once

    with pytest.raises(ValueError, match="binding"):
        read_label_manifest_once(
            packages["labels"] / "labels.json",
            expected_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
            expected_runtime_manifest_hash="1" * 64,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical spelling semantics")
def test_canonical_locator_rejects_case_alias(tmp_path):
    from backend.benchmarks.rcaeval.ledger import canonical_locator

    actual = tmp_path / "ReviewRoot" / "Single"
    actual.mkdir(parents=True)
    alias = Path(str(actual).lower())
    with pytest.raises(ValueError, match="canonical|locator"):
        canonical_locator(alias)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter semantics")
def test_canonical_locator_rejects_drive_letter_alias(tmp_path):
    from backend.benchmarks.rcaeval.ledger import canonical_locator

    drive, rest = os.path.splitdrive(str(tmp_path))
    assert drive and drive[0].isupper()
    alias = Path(drive.lower() + rest)
    with pytest.raises(ValueError, match="canonical|locator"):
        canonical_locator(alias)


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC semantics")
def test_canonical_locator_rejects_unc_paths():
    from backend.benchmarks.rcaeval.ledger import canonical_locator

    with pytest.raises(ValueError, match="UNC|locator"):
        canonical_locator(Path(r"\\definitely-not-a-real-host-xyz\share\inexistent"))


def test_canonical_locator_rejects_missing_paths(tmp_path):
    from backend.benchmarks.rcaeval.ledger import canonical_locator

    with pytest.raises(ValueError, match="exist|locator"):
        canonical_locator(tmp_path / "missing" / "leaf")


def test_canonical_locator_accepts_normal_spelling(tmp_path):
    from backend.benchmarks.rcaeval.ledger import canonical_locator

    assert canonical_locator(tmp_path) == os.path.normpath(str(tmp_path.resolve()))


def test_canonical_creation_locator_binds_existing_ancestor(tmp_path):
    from backend.benchmarks.rcaeval.ledger import canonical_creation_locator

    pending = tmp_path / "pending" / "leaf"
    expected = os.path.normpath(str(tmp_path.resolve() / "pending" / "leaf"))
    assert canonical_creation_locator(pending) == expected
    with pytest.raises(ValueError, match="absolute|traversal|locator"):
        canonical_creation_locator(Path("relative-output"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_canonical_creation_locator_rejects_junction_ancestor(tmp_path):
    from backend.benchmarks.rcaeval.ledger import (
        canonical_creation_locator,
        canonical_locator,
    )

    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="reparse|junction|symlink|canonical"):
        canonical_locator(junction)
    with pytest.raises(ValueError, match="reparse|junction|symlink|canonical"):
        canonical_creation_locator(junction / "leaf")


def _frozen_prediction_root(
    root: Path, bundle_payloads: dict[str, bytes] | None = None
) -> str:
    """构造两侧已冻结的 prediction root 并返回 root checksum（SHA256SUMS 字节哈希）。"""
    for name in ("multi_intended", "single_intended"):
        side = root / name
        side.mkdir(parents=True, exist_ok=True)
        if bundle_payloads is not None and name in bundle_payloads:
            (side / "predictions.json").write_bytes(bundle_payloads[name])
        elif not (side / "predictions.json").exists():
            (side / "predictions.json").write_text(
                f'{{"configuration": "{name}"}}', encoding="utf-8"
            )
        digest = hashlib.sha256((side / "predictions.json").read_bytes()).hexdigest()
        (side / "SHA256SUMS").write_text(
            f"{digest}  predictions.json\n", encoding="utf-8"
        )
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if path.is_file() and path != root / "SHA256SUMS":
            relative = path.relative_to(root).as_posix()
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = [f"{digest}  {relative}" for relative, digest in sorted(entries)]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    (root / "SHA256SUMS").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_child_frozen_root_verification_binds_canonical_bytes(tmp_path):
    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(root)
    verified = verify_frozen_prediction_root(root, root_hash)
    assert verified["single_intended/predictions.json"].startswith(b'{"configuration"')
    assert "multi_intended/SHA256SUMS" in verified
    with pytest.raises(ValueError, match="sha256|hash|checksum"):
        verify_frozen_prediction_root(root, "0" * 64)


def test_child_frozen_root_rejects_post_freeze_replacement(tmp_path):
    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(root)
    (root / "single_intended" / "predictions.json").write_text(
        '{"tampered": true}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="changed|mismatch|checksum|tamper"):
        verify_frozen_prediction_root(root, root_hash)


def test_child_frozen_root_rejects_replaced_checksum_manifest(tmp_path):
    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(root)
    (root / "single_intended" / "predictions.json").write_text(
        '{"tampered": 1}', encoding="utf-8"
    )
    # 攻击者在 freeze 后同时替换 bundle 与 SHA256SUMS；root 哈希绑定必须拒绝
    _frozen_prediction_root(root)
    with pytest.raises(ValueError, match="hash|checksum"):
        verify_frozen_prediction_root(root, root_hash)


def test_child_frozen_root_rejects_extra_and_missing_entries(tmp_path):
    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(root)
    (root / "single_intended" / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="set|extra|missing|checksum|changed"):
        verify_frozen_prediction_root(root, root_hash)
    (root / "single_intended" / "extra.json").unlink()
    (root / "single_intended" / "predictions.json").unlink()
    with pytest.raises(ValueError, match="set|extra|missing|checksum|changed"):
        verify_frozen_prediction_root(root, root_hash)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_frozen_root_verification_fails_fast_on_junction_loop(tmp_path):
    """冻结 root 内指向祖先的 junction 环必须快速 fail-closed：rglob 递归
    进入 junction（junction 非 symlink）指数膨胀，先物化的 sorted(rglob)
    会挂起而不是拒绝。"""
    import time

    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(root)
    for name in ("loop1", "loop2"):
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / name), str(root)],
            check=True,
            capture_output=True,
        )
    started = time.monotonic()
    with pytest.raises(ValueError, match="reparse|junction|symlink"):
        verify_frozen_prediction_root(root, root_hash)
    assert time.monotonic() - started < 5.0, "junction loop must fail fast, not hang"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_package_checksum_verification_fails_fast_on_junction_loop(tmp_path):
    """_verify_checksums 同款 sorted(rglob)：package 内 junction 环快速拒绝。"""
    import time

    from backend.benchmarks.rcaeval.isolation import _verify_checksums

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _write_checksums(package)
    for name in ("loop1", "loop2"):
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(package / name), str(package)],
            check=True,
            capture_output=True,
        )
    started = time.monotonic()
    with pytest.raises(ValueError, match="reparse|junction|symlink"):
        _verify_checksums(package)
    assert time.monotonic() - started < 5.0, "junction loop must fail fast, not hang"


def test_checksum_parser_rejects_posix_rooted_paths():
    """M3 拒绝 absolute 必须跨平台一致：Windows 下 Path('/etc/a.txt') 非
    absolute、无 drive、as_posix 往返一致，parser 必须显式拒绝 rooted 拼写。"""
    from backend.benchmarks.rcaeval.isolation import parse_canonical_checksum_bytes

    digest = hashlib.sha256(b"{}").hexdigest()
    for rooted in ("/etc/a.txt", "//etc/a.txt", "/a.txt"):
        raw = f"{digest}  {rooted}\n".encode()
        with pytest.raises(ValueError, match="canonical|absolute|rooted"):
            parse_canonical_checksum_bytes(raw)


def test_child_frozen_root_rejects_noncanonical_checksum_paths(tmp_path):
    from backend.benchmarks.rcaeval.isolation import verify_frozen_prediction_root

    root = tmp_path / "predictions"
    _frozen_prediction_root(root)
    sums_path = root / "SHA256SUMS"
    canonical = sums_path.read_bytes().decode("utf-8")
    aliased = canonical.replace("  single_intended/", "  ./single_intended/")
    assert aliased != canonical
    sums_path.write_bytes(aliased.encode("utf-8"))
    aliased_hash = hashlib.sha256(sums_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="canonical|checksum|path"):
        verify_frozen_prediction_root(root, aliased_hash)


def _evaluator_argv(root: Path, root_hash: str, tmp_path: Path) -> list[str]:
    return [
        "evaluator",
        "--bundle",
        str(root / "single_intended" / "predictions.json"),
        "--bundle",
        str(root / "multi_intended" / "predictions.json"),
        "--labels",
        str(tmp_path / "labels" / "labels.json"),
        "--output",
        str(tmp_path / "out" / "evaluation.json"),
        "--ledger",
        str(tmp_path / "pair-ledger.sqlite3"),
        "--custodian-manifest",
        str(tmp_path / "custodian-manifest.json"),
        "--partition",
        "tt90",
        "--prediction-set-hash",
        root_hash,
        "--audit-export",
        str(tmp_path / "audit.json"),
        "--manual-audit",
        str(tmp_path / "manual.json"),
        "--label-open-token",
        "token",
        "--expected-label-manifest-hash",
        "1" * 64,
        "--expected-runtime-manifest-hash",
        "2" * 64,
    ]


def test_evaluator_child_rejects_post_freeze_bundle_before_any_artifact(
    tmp_path, monkeypatch
):
    from backend.benchmarks.rcaeval import evaluator

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(root)
    # launch spec 创建后替换 predictions.json：spec 哈希不变，child 必须独立复验
    (root / "single_intended" / "predictions.json").write_text(
        '{"tampered": true}', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", _evaluator_argv(root, root_hash, tmp_path))
    with pytest.raises(ValueError, match="checksum|changed|mismatch|tamper"):
        evaluator.main()
    assert not (tmp_path / "out" / "evaluation.json").exists()


def _parseable_bundle_bytes(configuration: RcaEvalConfiguration) -> bytes:
    """最小可解析 PredictionBundle 字节；评分/cardinality gate 不在本测试面内。"""
    capability = EndpointCapabilityIdentity(
        provider="fixture",
        model="fixture-model",
        api_mode="responses",
        endpoint_id="fixture",
        artifact_hash="3" * 64,
    )
    identity = FrozenRunIdentity(
        source_commit="1" * 40,
        source_manifest_hash="c" * 64,
        runtime_manifest_hash="2" * 64,
        capability=capability,
        prompt_hash="4" * 64,
        tool_manifest_hash="5" * 64,
        skill_catalog_hash="6" * 64,
        prediction_schema_hash="7" * 64,
        normalizer_hash="8" * 64,
        scorer_dependency_hash="9" * 64,
        dependency_lock_hash="a" * 64,
        memory_snapshot_hash="b" * 64,
        retry_policy_hash="d" * 64,
    )
    budget = EvaluationBudget(
        configuration=configuration,
        token_budget=4_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
        max_investigators=1,
        max_rounds=1,
    )
    bundle = PredictionBundle(
        partition=RcaEvalPartition.TT90,
        configuration=configuration,
        identity=identity,
        budget=budget,
        predictions=[],
        frozen_at=datetime(2026, 8, 10, tzinfo=UTC),
        bundle_hash="e" * 64,
    )
    return bundle.model_dump_json().encode("utf-8")


def test_evaluator_child_binds_label_hash_through_custodian_fence(tmp_path, monkeypatch):
    """H1 wiring: child 把 CLI 期望哈希交给 custodian fence，而不是直接相信。"""
    from backend.benchmarks.rcaeval import evaluator

    root = tmp_path / "predictions"
    root_hash = _frozen_prediction_root(
        root,
        bundle_payloads={
            "single_intended": _parseable_bundle_bytes(
                RcaEvalConfiguration.SINGLE_INTENDED
            ),
            "multi_intended": _parseable_bundle_bytes(
                RcaEvalConfiguration.MULTI_INTENDED
            ),
        },
    )
    captured = {}

    class Sentinel(Exception):
        pass

    fake_ledger = SimpleNamespace(
        path=(tmp_path / "pair-ledger.sqlite3").resolve(),
    )

    def capture_assert(**kwargs):
        captured.update(kwargs)
        raise Sentinel

    fake_ledger.assert_label_open = capture_assert
    monkeypatch.setattr(
        evaluator.CustodianPairLedger,
        "from_manifest",
        classmethod(lambda cls, path: fake_ledger),
    )
    # 以下 gate 各有专属测试（scorer 身份、prelabel audit、formal cardinality）；
    # 本测试只隔离 H1 接线：CLI 期望哈希必须原样进入 custodian fence。
    monkeypatch.setattr(evaluator, "_validate_scorer_dependency_identity", lambda b: None)
    monkeypatch.setattr(evaluator, "validate_prelabel_audit", lambda *a, **k: None)
    monkeypatch.setattr(evaluator, "_validate_formal_configuration_set", lambda *a: None)
    monkeypatch.setattr(evaluator, "_validate_frozen_bundle", lambda b: None)
    monkeypatch.setattr(
        evaluator, "_validate_formal_bundle_cardinality", lambda *a: None
    )
    monkeypatch.setattr(
        evaluator.EvidenceAuditExport,
        "model_validate_json",
        classmethod(lambda cls, raw: SimpleNamespace(export_hash="f" * 64)),
    )
    monkeypatch.setattr(
        evaluator.ManualAuditArtifact,
        "model_validate_json",
        classmethod(lambda cls, raw: SimpleNamespace(artifact_hash="0" * 64)),
    )
    (tmp_path / "audit.json").write_text("{}", encoding="utf-8")
    (tmp_path / "manual.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", _evaluator_argv(root, root_hash, tmp_path))
    with pytest.raises(Sentinel):
        evaluator.main()
    assert captured["expected_label_manifest_hash"] == "1" * 64
    assert captured["prediction_set_hash"] == root_hash
    assert captured["partition"] == "tt90"
    assert not (tmp_path / "out" / "evaluation.json").exists()


def test_evaluator_launch_rejects_malformed_expected_runtime_hash(packages):
    with pytest.raises(ValueError, match="sha256"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash=_bundle_hash(packages["predictions"]),
            label_package=packages["labels"],
            argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
            expected_runtime_manifest_hash="z" * 64,
            expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
        )


def test_evaluator_launch_happy_path(packages):
    spec = build_evaluator_launch(
        predictions_bundle=packages["predictions"],
        expected_bundle_hash=_bundle_hash(packages["predictions"]),
        label_package=packages["labels"],
        argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
        expected_runtime_manifest_hash=_runtime_hash(),
        expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
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


def test_evaluator_child_rejects_label_hash_mismatch_before_artifact(packages):
    """The trusted child validates the custodian hash on its sole label read."""
    from backend.benchmarks.rcaeval.evaluator import read_label_manifest_once

    with pytest.raises(ValueError, match="label.*hash|label.*manifest"):
        read_label_manifest_once(
            packages["labels"] / "labels.json",
            expected_manifest_hash="0" * 64,
            expected_runtime_manifest_hash=_runtime_hash(),
        )


def test_evaluator_child_rejects_label_path_replacement_before_binding(packages, monkeypatch):
    import os

    import backend.benchmarks.rcaeval.evaluator as evaluator

    labels_path = packages["labels"] / "labels.json"
    backup_path = packages["labels"] / "labels.original.json"
    replacement_path = packages["labels"] / "labels.replacement.json"
    replacement_path.write_bytes(b"{}\n")
    original_open = os.open
    swapped = False

    def swap_before_open(path, *args):
        nonlocal swapped
        if Path(path) == labels_path and not swapped:
            labels_path.replace(backup_path)
            replacement_path.replace(labels_path)
            swapped = True
        return original_open(path, *args)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(ValueError, match="changed|replaced|label"):
        evaluator.read_label_manifest_once(
            labels_path,
            expected_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
            expected_runtime_manifest_hash=_runtime_hash(),
        )


def test_runtime_checksum_manifest_rejects_duplicate_entries(packages):
    sums = packages["runtime"] / "SHA256SUMS"
    original = sums.read_text(encoding="utf-8")
    sums.write_text(original + original.splitlines()[0] + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate|canonical|checksum"):
        build_prediction_launch(
            runtime_package=packages["runtime"],
            predictions_dir=packages["predictions"],
            argv=_argv(),
            environ={},
        )


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
            expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
        )


def test_evaluator_launch_rejects_wrong_expected_hash(packages):
    with pytest.raises(ValueError, match="hash"):
        build_evaluator_launch(
            predictions_bundle=packages["predictions"],
            expected_bundle_hash="0" * 64,
            label_package=packages["labels"],
            argv=["python", "-m", "backend.benchmarks.rcaeval.evaluator"],
            expected_runtime_manifest_hash=_runtime_hash(),
            expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
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
            expected_label_manifest_hash=_label_manifest(_runtime_hash()).manifest_hash,
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
                    custodian_manifest=tmp_path / "custodian-manifest.json",
            )
        )

    from backend.benchmarks.rcaeval.ledger import CustodianPairLedger, LedgerState

    snapshot = CustodianPairLedger(
        predictions_root.parent / "pair-ledger.sqlite3",
        custodian_manifest=tmp_path / "custodian-manifest.json",
    ).snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value


def test_prediction_worker_rejects_direct_unisolated_entry(monkeypatch):
    monkeypatch.delenv("RCAEVAL_PREDICTION_CHILD", raising=False)

    with pytest.raises(ValueError, match="launch-predict"):
        _predict(SimpleNamespace())


def test_prediction_launcher_rejects_relative_persisted_locator_before_setup(tmp_path):
    with pytest.raises(ValueError, match="absolute|canonical|locator"):
        _launch_predict(
            SimpleNamespace(
                output=Path("relative-output"),
                pair_root=tmp_path / "pair-root",
            )
        )


def test_prediction_worker_imports_from_external_cwd_without_editable_install(tmp_path):
    source_root = Path(__file__).resolve().parents[2]
    worker = source_root / "backend" / "benchmarks" / "rcaeval" / "prediction_worker.py"
    result = subprocess.run(
        [sys.executable, str(worker), "--source-root", str(source_root), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--runtime" in result.stdout


def test_prediction_launcher_sanitizes_child_and_never_passes_label_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.benchmarks.rcaeval import isolation
    from backend.benchmarks.rcaeval.ledger import create_custodian_manifest

    captured = {}

    def build_spec(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(argv=tuple(kwargs["argv"]), cwd=str(tmp_path), env={})

    output = tmp_path / "single_intended"
    pair_root = tmp_path / "pair-root"
    output = pair_root / "single_intended"

    class FakeProcess:
        def __init__(self, args, cwd=None, env=None):
            self.args = args
            self.returncode = None

        def wait(self, timeout=None):
            output.mkdir()
            (output / "SHA256SUMS").write_text("frozen\n", encoding="utf-8")
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(isolation, "build_prediction_launch", build_spec)
    monkeypatch.setattr(
        isolation,
        "verify_runtime_package",
        lambda _: SimpleNamespace(manifest_hash="1" * 64),
    )
    monkeypatch.setattr("subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._prediction_pair_identity",
        lambda _: "a" * 64,
    )
    # capability/key 准入与构造守卫有专项测试；本测试聚焦 launcher 隔离/ledger 语义
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._preflight_capability_admission",
        lambda _: None,
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._preflight_launch_construction",
        lambda _: None,
    )
    label_path = tmp_path / "labels"
    custodian_manifest = create_custodian_manifest(
        tmp_path,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )

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
                custodian_manifest=custodian_manifest,
        )
    )

    child_argv = [str(item) for item in captured["argv"]]
    assert str(label_path) not in child_argv
    assert captured["environ"]["RCAEVAL_PREDICTION_CHILD"] == "1"
    assert str(label_path) in captured["forbidden_locators"]


def test_prediction_completion_failure_invalidates_pair_without_future_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from backend.benchmarks.rcaeval import isolation
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        LedgerState,
        create_custodian_manifest,
    )

    pair_root = tmp_path / "pair-root"
    output = pair_root / "single_intended"
    label_path = tmp_path / "labels"
    manifest = create_custodian_manifest(
        tmp_path,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )

    monkeypatch.setattr(
        isolation,
        "build_prediction_launch",
        lambda **kwargs: SimpleNamespace(argv=tuple(kwargs["argv"]), cwd=str(tmp_path), env={}),
    )
    monkeypatch.setattr(
        isolation,
        "verify_runtime_package",
        lambda _: SimpleNamespace(manifest_hash="1" * 64),
    )
    class FakeProcess:
        def __init__(self, args, cwd=None, env=None):
            self.args = args
            self.returncode = None

        def wait(self, timeout=None):
            output.mkdir(parents=True)
            (output / "SHA256SUMS").write_text("frozen\n", encoding="utf-8")
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._prediction_pair_identity",
        lambda _: "a" * 64,
    )
    # capability/key 准入与构造守卫有专项测试；本测试聚焦 launcher 隔离/ledger 语义
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._preflight_capability_admission",
        lambda _: None,
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._preflight_launch_construction",
        lambda _: None,
    )

    def locked_completion(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(CustodianPairLedger, "record_side_completed", locked_completion)
    with pytest.raises(sqlite3.OperationalError):
        _launch_predict(
            SimpleNamespace(
                runtime=tmp_path / "runtime",
                pair_root=pair_root,
                custodian_manifest=manifest,
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
    snapshot = CustodianPairLedger.from_manifest(manifest).snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value


def test_prediction_launcher_heartbeats_lease_while_child_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """正式 30/90 例子进程运行远超默认 900s lease；子进程存活期间启动器必须
    持续续租，否则完成时 lease 过期会被收敛为 FAILED_NON_RESUMABLE。"""
    from backend.benchmarks.rcaeval import isolation
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        create_custodian_manifest,
    )

    pair_root = tmp_path / "pair-root"
    output = pair_root / "single_intended"
    label_path = tmp_path / "labels"
    manifest = create_custodian_manifest(
        tmp_path,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    monkeypatch.setattr(
        isolation,
        "build_prediction_launch",
        lambda **kwargs: SimpleNamespace(
            argv=("fake-child",), cwd=str(tmp_path), env={}
        ),
    )
    monkeypatch.setattr(
        isolation,
        "verify_runtime_package",
        lambda _: SimpleNamespace(manifest_hash="1" * 64),
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._prediction_pair_identity",
        lambda _: "a" * 64,
    )
    # capability/key 准入与构造守卫有专项测试；本测试聚焦 launcher 隔离/ledger 语义
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._preflight_capability_admission",
        lambda _: None,
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._preflight_launch_construction",
        lambda _: None,
    )

    heartbeats = 0
    real_heartbeat = CustodianPairLedger.heartbeat

    def counted_heartbeat(self, *args, **kwargs):
        nonlocal heartbeats
        heartbeats += 1
        return real_heartbeat(self, *args, **kwargs)

    monkeypatch.setattr(CustodianPairLedger, "heartbeat", counted_heartbeat)

    class FakeProcess:
        """子进程两次 wait 超时（模拟长运行）后才写出冻结产物。"""

        def __init__(self, args, cwd=None, env=None):
            self.args = args
            self.returncode = None
            self._timeouts = 2

        def wait(self, timeout=None):
            if self._timeouts:
                self._timeouts -= 1
                raise subprocess.TimeoutExpired(self.args, timeout)
            output.mkdir(parents=True)
            (output / "SHA256SUMS").write_text("frozen\n", encoding="utf-8")
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("subprocess.Popen", FakeProcess)

    _launch_predict(
        SimpleNamespace(
            runtime=tmp_path / "runtime",
            pair_root=pair_root,
            custodian_manifest=manifest,
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

    # 预约后 1 次 + 每次 wait 超时各 1 次；缺任一次续租即回退为旧行为。
    assert heartbeats == 3
    sides = CustodianPairLedger.from_manifest(manifest).side_snapshot()
    assert sides["single_intended"] == "completed"


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
        output_dir.mkdir(parents=True, exist_ok=True)
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
                    custodian_manifest=tmp_path / "custodian-manifest.json",
            )
        )

    from backend.benchmarks.rcaeval.ledger import CustodianPairLedger, LedgerState

    snapshot = CustodianPairLedger(
        predictions_root.parent / "pair-ledger.sqlite3",
        custodian_manifest=tmp_path / "custodian-manifest.json",
    ).snapshot()
    assert snapshot["state"] == LedgerState.FAILED_NON_RESUMABLE.value


def test_launch_preflight_failure_never_consumes_reauthorization_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # epoch-5/6 教训的核心保证：本地可判的预检失败必须发生在 reauthorize
    # （消耗一次性 token）之前；顺序回退即测试失败。
    from backend.benchmarks.rcaeval import isolation
    from backend.benchmarks.rcaeval.ledger import (
        CustodianPairLedger,
        create_custodian_manifest,
    )

    pair_root = tmp_path / "pair-root"
    output = pair_root / "single_intended"
    label_path = tmp_path / "labels"
    manifest = create_custodian_manifest(
        tmp_path,
        runtime_manifest_hash="1" * 64,
        label_manifest_hash="2" * 64,
    )
    monkeypatch.setattr(
        isolation,
        "verify_runtime_package",
        lambda _: SimpleNamespace(manifest_hash="1" * 64),
    )
    monkeypatch.setattr(
        "backend.benchmarks.rcaeval.__main__._prediction_pair_identity",
        lambda _: "a" * 64,
    )

    def forbidden_reauthorize(self, *args, **kwargs):
        raise AssertionError("reauthorize must not run after preflight failure")

    monkeypatch.setattr(CustodianPairLedger, "reauthorize", forbidden_reauthorize)
    monkeypatch.delenv("DIAGOPS_AGENTS_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DIAGOPS_AGENTS_API_KEY"):
        _launch_predict(
            SimpleNamespace(
                runtime=tmp_path / "runtime",
                pair_root=pair_root,
                custodian_manifest=manifest,
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
                reauthorization_token="epoch-test-token",
            )
        )
