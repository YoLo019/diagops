import hashlib
import json
from pathlib import Path

import pytest

from backend.benchmarks.rcaeval.models import (
    RcaEvalPartition,
    RcaEvalSystem,
    SourcePin,
    SystemTaxonomy,
)
from backend.benchmarks.rcaeval.prepare import (
    inspect_source,
    load_pin,
    prepare_partitions,
)

# 测试种子与 pin 字段取值均为合成 fixture 身份，不代表真实上游 artifact。
TEST_SEED = "diagops-v11-rcaeval-re2-test-seed"
TEST_REVISION = "0123456789abcdef0123456789abcdef01234567"
TEST_REPOSITORY = "https://example.invalid/synthetic/rcaeval-re2"

TAXONOMY = {
    RcaEvalSystem.ONLINE_BOUTIQUE: SystemTaxonomy(
        services=[f"ob-service-{index}" for index in range(5)],
        faults=[f"fault-{index}" for index in range(6)],
    ),
    RcaEvalSystem.SOCK_SHOP: SystemTaxonomy(
        services=[f"ss-service-{index}" for index in range(5)],
        faults=[f"fault-{index}" for index in range(6)],
    ),
    RcaEvalSystem.TRAIN_TICKET: SystemTaxonomy(
        services=[f"tt-service-{index}" for index in range(5)],
        faults=[f"fault-{index}" for index in range(6)],
    ),
}

EXPECTED_SOURCE_CASE_COUNT = 3 * 5 * 6 * 3


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _case_id(system: RcaEvalSystem, service: str, fault: str, repetition: int) -> str:
    return f"{system.value}-{service}-{fault}-r{repetition}"


def _telemetry_payload(case_id: str) -> bytes:
    # 遥测正文不得携带源 case ID（答案元数据）；fixture 只放普通数值列。
    del case_id
    return (
        b"timestamp,cpu,latency\n"
        b"1784001600,0.5,12.5\n"
        b"1784001660,0.7,15.0\n"
    )


def _write_case(
    source_root: Path,
    system: RcaEvalSystem,
    service: str,
    fault: str,
    repetition: int,
    *,
    telemetry_name: str = "metrics.csv",
    telemetry_payload: bytes | None = None,
    extra_fields: dict | None = None,
) -> str:
    case_id = _case_id(system, service, fault, repetition)
    case_dir = source_root / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    payload = telemetry_payload if telemetry_payload is not None else _telemetry_payload(case_id)
    (case_dir / telemetry_name).write_bytes(payload)
    descriptor = {
        "case_id": case_id,
        "system": system.value,
        "service": service,
        "fault": fault,
        "repetition": repetition,
        "files": [telemetry_name],
    }
    if extra_fields:
        descriptor.update(extra_fields)
    (case_dir / "case.json").write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return case_id


def _build_full_source(source_root: Path) -> None:
    for system, taxonomy in TAXONOMY.items():
        for service in taxonomy.services:
            for fault in taxonomy.faults:
                for repetition in (1, 2, 3):
                    _write_case(source_root, system, service, fault, repetition)


def _pin_from_source(source_root: Path) -> SourcePin:
    files: dict[str, str] = {}
    for path in sorted(source_root.rglob("*")):
        if path.is_file():
            files[path.relative_to(source_root).as_posix()] = _sha256_bytes(path.read_bytes())
    archive_payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return SourcePin(
        upstream_repository=TEST_REPOSITORY,
        upstream_revision=TEST_REVISION,
        upstream_artifact="synthetic-re2",
        archive_sha256=_sha256_bytes(archive_payload),
        selection_seed=TEST_SEED,
        taxonomy=TAXONOMY,
        files=files,
    )


def _write_pin(source_root: Path, pin_path: Path) -> SourcePin:
    pin = _pin_from_source(source_root)
    pin_path.write_text(
        pin.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return pin


@pytest.fixture(scope="module")
def source_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("rcaeval-source")
    _build_full_source(root)
    return root


@pytest.fixture(scope="module")
def pin_path(source_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pin") / "pin.json"
    _write_pin(source_root, path)
    return path


@pytest.fixture(scope="module")
def prepared(source_root: Path, pin_path: Path, tmp_path_factory: pytest.TempPathFactory):
    return prepare_partitions(source_root, pin_path, tmp_path_factory.mktemp("prepared"))


def _expected_selection(system: RcaEvalSystem) -> set[str]:
    taxonomy = TAXONOMY[system]
    selected: set[str] = set()
    for service in taxonomy.services:
        for fault in taxonomy.faults:
            candidates = [_case_id(system, service, fault, repetition) for repetition in (1, 2, 3)]
            selected.add(
                min(candidates, key=lambda item: _sha256_bytes(f"{TEST_SEED}:{item}".encode()))
            )
    return selected


def _package_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_prepare_partition_counts_and_cell_coverage(prepared):
    manifest = prepared.runtime_manifest
    assert manifest.partition_counts == {
        RcaEvalPartition.OB30: 30,
        RcaEvalPartition.SS30: 30,
        RcaEvalPartition.TT90: 90,
    }
    labels_by_partition: dict[RcaEvalPartition, list] = {
        partition: [] for partition in RcaEvalPartition
    }
    for entry in prepared.label_manifest.entries:
        labels_by_partition[entry.partition].append(entry)
    for partition, system in (
        (RcaEvalPartition.OB30, RcaEvalSystem.ONLINE_BOUTIQUE),
        (RcaEvalPartition.SS30, RcaEvalSystem.SOCK_SHOP),
    ):
        entries = labels_by_partition[partition]
        assert len(entries) == 30
        cells = {(entry.service, entry.fault) for entry in entries}
        assert cells == {
            (service, fault)
            for service in TAXONOMY[system].services
            for fault in TAXONOMY[system].faults
        }
        assert {entry.source_case_id for entry in entries} == _expected_selection(system)
        assert all(entry.repetition in (1, 2, 3) for entry in entries)
    tt_entries = labels_by_partition[RcaEvalPartition.TT90]
    assert len(tt_entries) == 90
    cell_repetitions: dict[tuple[str, str], set[int]] = {}
    for entry in tt_entries:
        cell_repetitions.setdefault((entry.service, entry.fault), set()).add(entry.repetition)
    assert cell_repetitions == {
        (service, fault): {1, 2, 3}
        for service in TAXONOMY[RcaEvalSystem.TRAIN_TICKET].services
        for fault in TAXONOMY[RcaEvalSystem.TRAIN_TICKET].faults
    }


def test_prepare_records_pinned_provenance(prepared, pin_path):
    pin = load_pin(pin_path)
    manifest = prepared.runtime_manifest
    assert manifest.source_class == "public_dataset"
    assert manifest.upstream_repository == TEST_REPOSITORY
    assert manifest.upstream_revision == TEST_REVISION
    assert manifest.upstream_artifact == "synthetic-re2"
    assert manifest.archive_sha256 == pin.archive_sha256
    assert manifest.selection_seed == TEST_SEED
    assert prepared.label_manifest.runtime_manifest_hash == manifest.manifest_hash


def test_prepare_is_byte_equivalent_on_repeat(source_root, pin_path, tmp_path):
    first = prepare_partitions(source_root, pin_path, tmp_path / "first")
    second = prepare_partitions(source_root, pin_path, tmp_path / "second")

    first_files = _package_files(first.output_dir)
    second_files = _package_files(second.output_dir)
    # custodian 锚点刻意绑定绝对 canonical root；runtime/label 包字节仍须跨输出根稳定。
    first_files.pop("custodian-manifest.json")
    second_files.pop("custodian-manifest.json")
    # seal HMAC key 是一次性随机秘密，不属于字节等价面；其随机性正是防伪造前提。
    first_files.pop("pair-ledger-seal.key")
    second_files.pop("pair-ledger-seal.key")
    assert first_files == second_files
    assert first.runtime_manifest.manifest_hash == second.runtime_manifest.manifest_hash
    assert first.label_manifest.manifest_hash == second.label_manifest.manifest_hash


def test_runtime_package_has_no_labels_or_source_ids(prepared):
    # M0-I2 / M0 exit review finding 1：service/fault 断言只覆盖 manifest 字节与
    # 文件名。真实 RE2 遥测正文合法包含服务名（15/15 抽样命中）与通用 fault 词元，
    # 对全包字节断言服务名不成立；泄漏判据是答案元数据（源 case ID、标签字段名、
    # 标签值出现在 manifest/文件名），不是遥测内容本身。
    runtime_files = _package_files(prepared.runtime_dir)
    blob = b"\n".join(runtime_files.values())
    metadata_blob = (prepared.runtime_dir / "manifest.json").read_bytes() + b"\n" + b"\n".join(
        name.encode() for name in runtime_files
    )
    for entry in prepared.label_manifest.entries:
        assert entry.source_case_id.encode() not in blob
        assert entry.service.encode() not in metadata_blob
        assert entry.fault.encode() not in metadata_blob
    manifest_text = (prepared.runtime_dir / "manifest.json").read_text(encoding="utf-8")
    assert '"service"' not in manifest_text
    assert '"fault"' not in manifest_text
    assert '"repetition"' not in manifest_text


def test_runtime_and_label_packages_have_zero_file_overlap(prepared):
    runtime_hashes = {
        _sha256_bytes(payload) for payload in _package_files(prepared.runtime_dir).values()
    }
    label_hashes = {
        _sha256_bytes(payload) for payload in _package_files(prepared.labels_dir).values()
    }
    assert runtime_hashes.isdisjoint(label_hashes)


def test_opaque_case_ids_are_stable_and_unlinkable(prepared):
    case_ids = [case.case_id for case in prepared.runtime_manifest.cases]
    assert len(case_ids) == 150
    assert len(set(case_ids)) == 150
    assert all(case_id.startswith("re2-") for case_id in case_ids)
    for case_id, entry in zip(
        sorted(case_ids),
        sorted(prepared.label_manifest.entries, key=lambda item: item.case_id),
        strict=True,
    ):
        assert entry.case_id == case_id
        expected = "re2-" + hashlib.sha256(
            f"{TEST_SEED}:opaque:{entry.partition.value}:{entry.source_case_id}".encode()
        ).hexdigest()[:16]
        assert case_id == expected


def test_prepare_rejects_changed_source_file(source_root, pin_path, tmp_path):
    target = source_root / "cases" / _case_id(
        RcaEvalSystem.ONLINE_BOUTIQUE, "ob-service-0", "fault-0", 1
    ) / "metrics.csv"
    original = target.read_bytes()
    target.write_bytes(original + b"tampered\n")
    try:
        with pytest.raises(ValueError, match="hash"):
            prepare_partitions(source_root, pin_path, tmp_path / "out")
    finally:
        target.write_bytes(original)


def test_prepare_rejects_extra_source_file(source_root, pin_path, tmp_path):
    extra = source_root / "cases" / "stray.txt"
    extra.write_text("stray", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="file set"):
            prepare_partitions(source_root, pin_path, tmp_path / "out")
    finally:
        extra.unlink()


def test_prepare_rejects_missing_cell(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    missing_dir = source / "cases" / _case_id(
        RcaEvalSystem.SOCK_SHOP, "ss-service-1", "fault-1", 2
    )
    for path in missing_dir.iterdir():
        path.unlink()
    missing_dir.rmdir()
    pin = _write_pin(source, tmp_path / "pin.json")
    assert pin is not None
    with pytest.raises(ValueError, match="missing cell"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def test_prepare_rejects_duplicate_cell(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    case_dir = source / "cases" / "sock-shop-duplicate-cell"
    case_dir.mkdir(parents=True)
    (case_dir / "metrics.csv").write_bytes(b"timestamp,cpu\n1784001600,0.1\n")
    (case_dir / "case.json").write_text(
        json.dumps(
            {
                "case_id": "sock-shop-duplicate-cell",
                "system": "sock_shop",
                "service": "ss-service-1",
                "fault": "fault-1",
                "repetition": 2,
                "files": ["metrics.csv"],
            }
        ),
        encoding="utf-8",
    )
    _write_pin(source, tmp_path / "pin.json")
    with pytest.raises(ValueError, match="duplicate cell"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def test_prepare_rejects_unexpected_label(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    case_json = source / "cases" / _case_id(
        RcaEvalSystem.TRAIN_TICKET, "tt-service-2", "fault-2", 1
    ) / "case.json"
    descriptor = json.loads(case_json.read_text(encoding="utf-8"))
    descriptor["fault"] = "fault-not-in-taxonomy"
    case_json.write_text(json.dumps(descriptor), encoding="utf-8")
    _write_pin(source, tmp_path / "pin.json")
    with pytest.raises(ValueError, match="unexpected label"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def _replace_case_telemetry(source: Path, telemetry_name: str, payload: bytes) -> None:
    """把固定 case 的遥测整体替换为给定探针文件，并同步 case.json 的 files 清单。"""
    case_id = _case_id(RcaEvalSystem.ONLINE_BOUTIQUE, "ob-service-3", "fault-3", 1)
    case_dir = source / "cases" / case_id
    for path in case_dir.iterdir():
        if path.name != "case.json":
            path.unlink()
    (case_dir / telemetry_name).write_bytes(payload)
    descriptor = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    descriptor["files"] = [telemetry_name]
    (case_dir / "case.json").write_text(json.dumps(descriptor), encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        # M0-R1：上游 istio-latency 分位列无有效样本时写出字面量 NaN，属于上游
        # 缺失值编码，放行（含大小写与空白变体）。
        b"timestamp,cpu\n1784001600,NaN\n",
        b"timestamp,cpu\n1784001600,nan\n1784001660, NAN \n",
        # M0-R2：16 位十六进制 trace/span ID（[0-9]+e[0-9]+ 形态）是标识符而非
        # 数值，不得再被 float 试探按科学计数法误拒。
        b"trace_id,span_id,duration\n96e3653877800818,00f067aa0ba902b7,12.5\n",
    ],
)
def test_prepare_accepts_nan_literal_and_identifier_cells(tmp_path, payload):
    source = tmp_path / "source"
    _build_full_source(source)
    _replace_case_telemetry(source, "metrics.csv", payload)
    _write_pin(source, tmp_path / "pin.json")
    result = prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")
    assert result.runtime_manifest.partition_counts[RcaEvalPartition.OB30] == 30


@pytest.mark.parametrize(
    ("telemetry_name", "payload"),
    [
        # M0-R1/M0-R2 修正案新合同：CSV 只拒绝显式 inf 系词元
        # （inf/infinity，任意大小写、可带符号与空白）。
        ("metrics.csv", b"timestamp,cpu\n1784001600,inf\n"),
        ("metrics.csv", b"timestamp,cpu\n1784001600,Infinity\n"),
        ("metrics.csv", b"timestamp,cpu\n1784001600,-INF\n"),
        ("metrics.csv", b"timestamp,cpu\n1784001600, +infinity \n"),
        # JSON 结构化常量无标识符歧义，行为锚定不变：NaN/Infinity/-Infinity 仍拒绝。
        ("series.json", b'{"value": NaN}'),
        ("series.json", b'{"value": Infinity}'),
        ("series.json", b'{"value": -Infinity}'),
    ],
)
def test_prepare_rejects_non_finite_telemetry(tmp_path, telemetry_name, payload):
    source = tmp_path / "source"
    _build_full_source(source)
    _replace_case_telemetry(source, telemetry_name, payload)
    _write_pin(source, tmp_path / "pin.json")
    with pytest.raises(ValueError, match="non-finite"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def test_prepare_rejects_source_case_id_inside_telemetry_content(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    case_id = _case_id(RcaEvalSystem.ONLINE_BOUTIQUE, "ob-service-4", "fault-4", 1)
    metrics = source / "cases" / case_id / "metrics.csv"
    metrics.write_bytes(metrics.read_bytes() + f"1784001720,0.9,20.0 # {case_id}\n".encode())
    _write_pin(source, tmp_path / "pin.json")
    with pytest.raises(ValueError, match="answer-bearing source case id"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def test_prepare_rejects_answer_bearing_telemetry_name(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    case_id = _case_id(RcaEvalSystem.ONLINE_BOUTIQUE, "ob-service-4", "fault-4", 1)
    case_dir = source / "cases" / case_id
    (case_dir / "metrics.csv").rename(case_dir / "answer.csv")
    descriptor = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    descriptor["files"] = ["answer.csv"]
    (case_dir / "case.json").write_text(json.dumps(descriptor), encoding="utf-8")
    _write_pin(source, tmp_path / "pin.json")
    with pytest.raises(ValueError, match="answer-bearing"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def test_prepare_rejects_path_traversal_in_case_files(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    case_id = _case_id(RcaEvalSystem.ONLINE_BOUTIQUE, "ob-service-0", "fault-1", 1)
    case_json = source / "cases" / case_id / "case.json"
    descriptor = json.loads(case_json.read_text(encoding="utf-8"))
    descriptor["files"] = ["../escape.csv"]
    case_json.write_text(json.dumps(descriptor), encoding="utf-8")
    _write_pin(source, tmp_path / "pin.json")
    with pytest.raises(ValueError, match="traversal|pattern"):
        prepare_partitions(source, tmp_path / "pin.json", tmp_path / "out")


def test_pin_rejects_malformed_revision_and_hash(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    pin = _pin_from_source(source)
    payload = pin.model_dump(mode="json")
    payload["upstream_revision"] = "not-a-commit-sha"
    bad_pin = tmp_path / "bad-pin.json"
    bad_pin.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_pin(bad_pin)
    payload = pin.model_dump(mode="json")
    payload["archive_sha256"] = "z" * 64
    bad_pin.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_pin(bad_pin)


def test_prepare_rejects_archive_hash_mismatch(tmp_path):
    source = tmp_path / "source"
    _build_full_source(source)
    pin = _pin_from_source(source)
    payload = pin.model_dump(mode="json")
    payload["archive_sha256"] = "0" * 64
    bad_pin = tmp_path / "pin.json"
    bad_pin.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="archive"):
        prepare_partitions(source, bad_pin, tmp_path / "out")


def test_prepare_refuses_non_empty_output(source_root, pin_path, tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing.txt").write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        prepare_partitions(source_root, pin_path, output)


def test_inspect_reports_selection_without_writing(source_root, pin_path, tmp_path):
    report = inspect_source(source_root, load_pin(pin_path))
    assert report.source_case_count == EXPECTED_SOURCE_CASE_COUNT
    assert report.partition_counts == {
        RcaEvalPartition.OB30: 30,
        RcaEvalPartition.SS30: 30,
        RcaEvalPartition.TT90: 90,
    }
    assert report.selected_source_ids[RcaEvalPartition.OB30] == sorted(
        _expected_selection(RcaEvalSystem.ONLINE_BOUTIQUE)
    )
    assert report.selected_source_ids[RcaEvalPartition.SS30] == sorted(
        _expected_selection(RcaEvalSystem.SOCK_SHOP)
    )
    assert len(report.selected_source_ids[RcaEvalPartition.TT90]) == 90
    assert not (tmp_path / "out").exists()


def test_selector_matches_frozen_golden_vector(source_root, pin_path):
    golden_path = Path(__file__).parent / "fixtures" / "rcaeval-selector-golden-v1.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    report = inspect_source(source_root, load_pin(pin_path))

    assert golden["schema_version"] == "rcaeval-selector-golden-v1"
    assert golden["selection_seed"] == TEST_SEED
    assert {
        partition.value: source_ids
        for partition, source_ids in report.selected_source_ids.items()
        if partition in {RcaEvalPartition.OB30, RcaEvalPartition.SS30}
    } == golden["selected_source_ids"]
