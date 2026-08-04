"""RCAEval RE2 源归档的校验、分区选择与不透明重打包。

输入是仓库之外的源目录与 custodian pin（上游 revision、归档哈希、选择种子、
标签全集、逐文件 sha256）。prepare 先把源树与 pin 逐字节对账，再按
spec §11.1 的 seeded 选择器（每个 `5 services × 6 faults` 单元格取
SHA-256(seed + ":" + source_case_id) 最小者；Train Ticket 保留全部 3 次重复）
产出分离的 runtime 包与 evaluator-only 标签包。runtime 包只含不透明 ID 与
重命名后的遥测字节，标签（service/fault/repetition/source id）只进标签包。
全程无墙钟时间、无随机数，重复 prepare 字节等价。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

from backend.benchmarks.rcaeval.models import (
    SYSTEM_TO_PARTITION,
    LabelEntry,
    LabelManifest,
    RcaEvalPartition,
    RcaEvalSystem,
    RuntimeCaseEntry,
    RuntimeManifest,
    SourceCaseDescriptor,
    SourcePin,
)

# 分区契约：OB30/SS30 每单元格 1 例共 30 例，TT90 每单元格 3 次重复共 90 例。
EXPECTED_PARTITION_COUNTS: dict[RcaEvalPartition, int] = {
    RcaEvalPartition.OB30: 30,
    RcaEvalPartition.SS30: 30,
    RcaEvalPartition.TT90: 90,
}
REPETITIONS_PER_CELL = 3

# 遥测只接受这三种后缀；CSV/JSON 做非有限数值扫描，LOG 只做 UTF-8/NUL 检查。
ALLOWED_TELEMETRY_SUFFIXES = frozenset({".csv", ".json", ".log"})
# 文件名即可泄露答案的遥测直接拒收，防止答案经 runtime 包文件名侧漏。
ANSWER_BEARING_STEMS = frozenset(
    {"answer", "answers", "label", "labels", "expected", "groundtruth", "ground_truth",
     "record", "solution"}
)
# 有界输入面：单文件超过 256 MiB 视为异常输入而非正常遥测。
# plan §10 将原 64 MiB 上限定为"provisional pending real-artifact reconciliation"：
# 真实 RE2 artifact 对账实测最大单文件 233,353,404 B
# （ts-route-service_socket_1/traces.csv，TT traces/logs 普遍 100–235 MB，共 110
# 文件超过 100 MB），64 MiB 在真实数据上不成立，按实测最大值向上取 2 的幂定为
# 256 MiB；pin 已逐文件钉住哈希，该上限只是异常输入的兜底防线。
MAX_TELEMETRY_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class PrepareResult:
    runtime_manifest: RuntimeManifest
    label_manifest: LabelManifest
    output_dir: Path
    runtime_dir: Path
    labels_dir: Path
    # 仅供进程内审计/测试使用；源 ID 不会写入 runtime 包任何字节。
    selection: dict[RcaEvalPartition, tuple[str, ...]]


@dataclass(frozen=True)
class InspectionReport:
    source_case_count: int
    partition_counts: dict[RcaEvalPartition, int]
    selected_source_ids: dict[RcaEvalPartition, list[str]]


@dataclass(frozen=True)
class _SourceCase:
    descriptor: SourceCaseDescriptor
    case_dir: Path

    @property
    def case_id(self) -> str:
        return self.descriptor.case_id


def load_pin(pin_path: Path) -> SourcePin:
    payload = json.loads(pin_path.read_bytes().decode("utf-8"))
    return SourcePin.model_validate(payload)


def inspect_source(source_root: Path, pin: SourcePin) -> InspectionReport:
    """只校验与预览选择结果，不写任何文件。"""
    source_root = source_root.resolve()
    _verify_source_tree(source_root, pin)
    cases = _load_cases(source_root, pin)
    selection = _select_partitions(cases, pin.selection_seed)
    return InspectionReport(
        source_case_count=len(cases),
        partition_counts={partition: len(ids) for partition, ids in selection.items()},
        selected_source_ids={partition: list(ids) for partition, ids in selection.items()},
    )


def prepare_partitions(source_root: Path, pin_path: Path, output_dir: Path) -> PrepareResult:
    pin = load_pin(pin_path)
    source_root = source_root.resolve()
    _verify_source_tree(source_root, pin)
    cases = _load_cases(source_root, pin)
    selection = _select_partitions(cases, pin.selection_seed)

    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output directory must be empty: refuse to overwrite")

    runtime_dir = output_dir / "runtime"
    labels_dir = output_dir / "labels"
    by_id = {case.case_id: case for case in cases}

    runtime_cases: list[RuntimeCaseEntry] = []
    label_entries: list[LabelEntry] = []
    for partition in RcaEvalPartition:
        for source_id in selection[partition]:
            case = by_id[source_id]
            opaque_id = _opaque_case_id(pin.selection_seed, partition, source_id)
            renamed = _materialize_runtime_case(case, runtime_dir, opaque_id)
            runtime_cases.append(
                RuntimeCaseEntry(case_id=opaque_id, partition=partition, files=renamed)
            )
            descriptor = case.descriptor
            label_entries.append(
                LabelEntry(
                    case_id=opaque_id,
                    partition=partition,
                    source_case_id=descriptor.case_id,
                    system=descriptor.system,
                    service=descriptor.service,
                    fault=descriptor.fault,
                    repetition=descriptor.repetition,
                )
            )

    runtime_cases.sort(key=lambda item: item.case_id)
    label_entries.sort(key=lambda item: item.case_id)
    runtime_manifest = RuntimeManifest(
        upstream_repository=pin.upstream_repository,
        upstream_revision=pin.upstream_revision,
        upstream_artifact=pin.upstream_artifact,
        archive_sha256=pin.archive_sha256,
        selection_seed=pin.selection_seed,
        partition_counts={partition: len(ids) for partition, ids in selection.items()},
        cases=runtime_cases,
    )
    runtime_manifest.manifest_hash = _manifest_hash(runtime_manifest)
    label_manifest = LabelManifest(
        runtime_manifest_hash=runtime_manifest.manifest_hash,
        entries=label_entries,
    )
    label_manifest.manifest_hash = _manifest_hash(label_manifest)

    _write_json(runtime_dir / "manifest.json", runtime_manifest.model_dump(mode="json"))
    _write_checksums(runtime_dir)
    _write_json(labels_dir / "labels.json", label_manifest.model_dump(mode="json"))
    _write_checksums(labels_dir)
    return PrepareResult(
        runtime_manifest=runtime_manifest,
        label_manifest=label_manifest,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        labels_dir=labels_dir,
        selection=selection,
    )


def _verify_source_tree(source_root: Path, pin: SourcePin) -> None:
    """源树必须与 pin 完全一致：文件集合、逐文件哈希、归档哈希三重对账。"""
    if not source_root.is_dir():
        raise ValueError(f"source root does not exist: {source_root}")
    actual: dict[str, str] = {}
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"source contains a symlink, refusing: {path}")
        if path.is_file():
            actual[path.relative_to(source_root).as_posix()] = _sha256_bytes(path.read_bytes())
    if set(actual) != set(pin.files):
        missing = sorted(set(pin.files) - set(actual))[:5]
        extra = sorted(set(actual) - set(pin.files))[:5]
        raise ValueError(f"source file set differs from pin (missing={missing}, extra={extra})")
    for relative, expected in pin.files.items():
        if actual[relative] != expected:
            raise ValueError(f"source file hash mismatch (changed source): {relative}")
    archive_hash = _canonical_sha256(pin.files)
    if archive_hash != pin.archive_sha256:
        raise ValueError("archive hash mismatch: pin archive identity does not match its files")


def _load_cases(source_root: Path, pin: SourcePin) -> list[_SourceCase]:
    descriptors: list[_SourceCase] = []
    case_json_paths = sorted(
        relative
        for relative in pin.files
        if relative.count("/") == 2 and relative.endswith("/case.json")
    )
    for relative in case_json_paths:
        case_dir = source_root / relative.rsplit("/", 1)[0]
        descriptor = SourceCaseDescriptor.model_validate(
            json.loads((source_root / relative).read_bytes().decode("utf-8"))
        )
        if descriptor.case_id != case_dir.name:
            raise ValueError(f"case id {descriptor.case_id!r} does not match its directory")
        _validate_labels(descriptor, pin)
        listed = set(descriptor.files)
        on_disk = {
            path.name
            for path in case_dir.iterdir()
            if path.is_file() and path.name != "case.json"
        }
        if listed != on_disk:
            raise ValueError(
                f"case {descriptor.case_id}: telemetry files differ from case.json listing"
            )
        for name in sorted(listed):
            _validate_telemetry(case_dir / name, descriptor.case_id)
        descriptors.append(_SourceCase(descriptor=descriptor, case_dir=case_dir))
    _validate_cells(descriptors, pin)
    return descriptors


def _validate_labels(descriptor: SourceCaseDescriptor, pin: SourcePin) -> None:
    taxonomy = pin.taxonomy[descriptor.system]
    if descriptor.service not in taxonomy.services or descriptor.fault not in taxonomy.faults:
        raise ValueError(
            f"unexpected label in case {descriptor.case_id}: "
            f"{descriptor.service}/{descriptor.fault} outside pinned taxonomy"
        )


def _validate_cells(cases: list[_SourceCase], pin: SourcePin) -> None:
    """每个系统必须恰好覆盖 `5 services × 6 faults × 3 repetitions`，缺一拒、重一拒。"""
    by_cell: dict[tuple[RcaEvalSystem, str, str, int], list[str]] = {}
    for case in cases:
        descriptor = case.descriptor
        key = (descriptor.system, descriptor.service, descriptor.fault, descriptor.repetition)
        by_cell.setdefault(key, []).append(descriptor.case_id)
    for key, ids in by_cell.items():
        if len(ids) > 1:
            raise ValueError(f"duplicate cell {key}: cases {sorted(ids)}")
    present = set(by_cell)
    for system, taxonomy in pin.taxonomy.items():
        missing = [
            (service, fault, repetition)
            for service in taxonomy.services
            for fault in taxonomy.faults
            for repetition in range(1, REPETITIONS_PER_CELL + 1)
            if (system, service, fault, repetition) not in present
        ]
        if missing:
            raise ValueError(f"missing cell(s) in {system.value}: {missing[:5]}")


def _validate_telemetry(path: Path, case_id: str) -> None:
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_TELEMETRY_SUFFIXES:
        raise ValueError(f"case {case_id}: unsupported telemetry suffix: {path.name}")
    if path.stem.lower() in ANSWER_BEARING_STEMS:
        raise ValueError(f"case {case_id}: answer-bearing telemetry name: {path.name}")
    payload = path.read_bytes()
    if len(payload) > MAX_TELEMETRY_BYTES:
        raise ValueError(f"case {case_id}: telemetry file exceeds size bound: {path.name}")
    if suffix == ".log":
        text = _decode_utf8(payload, case_id, path.name)
        if "\x00" in text:
            raise ValueError(f"case {case_id}: log contains NUL bytes: {path.name}")
    elif suffix == ".csv":
        text = _decode_utf8(payload, case_id, path.name)
        _reject_non_finite_csv(text, case_id, path.name)
    else:
        text = _decode_utf8(payload, case_id, path.name)
        _reject_non_finite_json(text, case_id, path.name)
    # 源 case ID 编码了 service/fault/repetition，属于答案元数据；
    # 遥测正文中出现完整 case ID 串视为答案侧漏，fail closed。
    if case_id in text:
        raise ValueError(f"case {case_id}: answer-bearing source case id in {path.name}")


def _decode_utf8(payload: bytes, case_id: str, name: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"case {case_id}: telemetry is not UTF-8: {name}") from exc


# M0-R1/M0-R2 修正案：CSV 只拒绝显式 inf 系词元（inf/infinity，任意大小写、
# 可带符号），不再对任意单元格做 float 试探。上游 metrics.csv/simple_metrics.csv
# 的 istio-latency 分位列在无有效样本时写出字面量 NaN（119/270 case），属于上游
# 缺失值编码，放行；traces.csv 的 16 位十六进制 trace/span ID（如
# 96e3653877800818，[0-9]+e[0-9]+ 形态）若按 float 解析会被当成科学计数法溢出为
# inf 而误拒（180/180 traces.csv），标识符列不是数值，天然不应进入数值校验。
_CSV_INF_TOKENS = frozenset(
    {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
)


def _reject_non_finite_csv(text: str, case_id: str, name: str) -> None:
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            if cell.strip().lower() in _CSV_INF_TOKENS:
                raise ValueError(f"case {case_id}: non-finite telemetry value in {name}")


def _reject_non_finite_json(text: str, case_id: str, name: str) -> None:
    def _reject_constant(constant: str) -> None:
        raise ValueError(
            f"case {case_id}: non-finite telemetry value in {name}: {constant}"
        )

    json.loads(text, parse_constant=_reject_constant)


def _select_partitions(
    cases: list[_SourceCase], seed: str
) -> dict[RcaEvalPartition, tuple[str, ...]]:
    """seeded 选择器：OB/SS 每单元格取哈希最小的一例，TT 保留全部重复。"""
    by_system: dict[RcaEvalSystem, list[_SourceCase]] = {system: [] for system in RcaEvalSystem}
    for case in cases:
        by_system[case.descriptor.system].append(case)

    selection: dict[RcaEvalPartition, tuple[str, ...]] = {}
    for system, partition in SYSTEM_TO_PARTITION.items():
        if partition is RcaEvalPartition.TT90:
            selection[partition] = tuple(sorted(case.case_id for case in by_system[system]))
            continue
        cells: dict[tuple[str, str], list[_SourceCase]] = {}
        for case in by_system[system]:
            key = (case.descriptor.service, case.descriptor.fault)
            cells.setdefault(key, []).append(case)
        chosen = [
            min(
                cell_cases,
                key=lambda case: _sha256_bytes(f"{seed}:{case.case_id}".encode()),
            ).case_id
            for cell_cases in cells.values()
        ]
        selection[partition] = tuple(sorted(chosen))
    for partition, ids in selection.items():
        if len(ids) != EXPECTED_PARTITION_COUNTS[partition]:
            raise ValueError(
                f"partition {partition.value} selected {len(ids)} cases, "
                f"expected {EXPECTED_PARTITION_COUNTS[partition]}"
            )
    return selection


def _opaque_case_id(seed: str, partition: RcaEvalPartition, source_case_id: str) -> str:
    digest = _sha256_bytes(f"{seed}:opaque:{partition.value}:{source_case_id}".encode())
    return f"re2-{digest[:16]}"


def _materialize_runtime_case(case: _SourceCase, runtime_dir: Path, opaque_id: str) -> list[str]:
    """遥测按原名排序重命名为 telemetry-NN.<suffix>，避免文件名携带服务/故障标签。"""
    case_out = runtime_dir / "cases" / opaque_id
    case_out.mkdir(parents=True, exist_ok=False)
    renamed: list[str] = []
    for index, name in enumerate(sorted(case.descriptor.files)):
        target_name = f"telemetry-{index:02d}{Path(name).suffix.lower()}"
        (case_out / target_name).write_bytes((case.case_dir / name).read_bytes())
        renamed.append(target_name)
    return renamed


def _manifest_hash(manifest: RuntimeManifest | LabelManifest) -> str:
    return _canonical_sha256(manifest.model_dump(mode="json", exclude={"manifest_hash"}))


def _write_checksums(package_dir: Path) -> None:
    lines = []
    for path in sorted(package_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(package_dir).as_posix()
            lines.append(f"{_sha256_bytes(path.read_bytes())}  {relative}")
    target = package_dir / "SHA256SUMS"
    with target.open("w", encoding="utf-8", newline="\n") as file:
        file.write("\n".join(lines) + "\n")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
