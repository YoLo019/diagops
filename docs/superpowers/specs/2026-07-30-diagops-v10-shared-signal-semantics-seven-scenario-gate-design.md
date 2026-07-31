# DiagOps V10 Shared Signal Semantics And Seven-Scenario Gate Design

Status: `approved`

Date: 2026-07-30

## 1. Context and decision

V10 已让 OpenRCA 与生产输入进入同一个确定性 Analyzer，但真实结果证明统一
`EvidenceItem` 字段还不足以形成统一诊断语义。

冻结六案例 targeted run
`D:\data\OpenRCA\results-v10-targeted-projector\run-20260730T090304525928Z`
完成 `6/6`，Evidence 引用有效率 `100%`，read-only violation、projection error
和 projection fallback 均为 `0`，但官方结果只有 `1/6` 行得到正分：

- task_1/time：`0/2`；
- task_2/reason：`0/2`；
- task_3/component：`1/2`。

只读根因调查确认：

1. `_metric_signal_type()` 先匹配 `container`，使
   `container_network_receive_packets_dropped` 被错误分类为 `process`；
2. baseline 为零时，不同 metric series 的原始 `deviation_score` 可达到
   `10^9`–`10^19`，全局 Top-N 实际由不可比数值支配；
3. `Market/cloudbed-1:17` 的 memory 候选从第 21 名开始，当前 Top-20 无法覆盖；
4. `Bank:107` 的 TCP wait/network metric 存在，但被映射为通用 `latency`，并被
   database metric 挤出；
5. time 案例的正确原始 observation 已存在，但峰值替换和跨段聚类丢失了真实
   onset；
6. 现有四场景 production Gate 已通过，只能证明 deployment、dependency、
   traffic 和 healthy control 不回归，不能证明 network、memory、process 的
   生产诊断准确率。

决策：采用共享纯函数 Signal Core。OpenRCA 与生产 Prometheus Provider 复用同一
taxonomy、异常段、onset、归一化强度和 family-balanced 候选选择。删除失败
Projector 中的第二套归因/排序，不在旧方案上追加权重或 case 规则。

本设计补充并收紧原 V10 shared-core 设计；它取代
`2026-07-30-openrca-task-aware-evidence-projection-design.md` 作为当前恢复工作的
主动规格。旧规格和失败工件作为历史证据保留，不重写或删除。

## 2. Confirmed Requirements Brief

```text
Decision: continue

Problem:
共享诊断核心存在 metric 误分类、异常强度不可比、Top-N 覆盖丢失和 onset
不准确，导致 OpenRCA time/reason/component 错误；现有生产 Gate 太窄。

Target user:
使用 DiagOps 做生产故障定位的 SRE。

Desired behavior:
OpenRCA 与生产共用 signal taxonomy、可比较的异常排序、onset 检测和
component/reason 归一化；适配器只转换原始 telemetry。

Scope:
共享内部 signal 归一化和异常序列逻辑；OpenRCA Metric/Dependency Provider 与
Prometheus Provider 复用该逻辑；简化 Projector；增加 network、memory、process
生产场景并校验 cause、component、reason、onset；在 live source checkpoint 前
完成已审计的全仓死代码、source-shape test、重复文档和 repository fallback 清理。

Non-goals:
不读 Ground Truth、不加 case-specific 规则、不引入 LLM/训练模型/可调权重框架、
不新增生产写操作、NET_ADMIN、数据库迁移或公共 API。

Acceptance:
OpenRCA 六案例至少 3/6 且 time/reason/component 各命中；生产旧四场景不回归，
新增三场景的 Top-1 cause、component、reason 正确且 onset 在 ±60 秒内；全部
Evidence 有效且 read-only violation 为零。

Resolved decisions:
共享方案 A；双 targeted Gate；七个生产场景；onset 容差 ±60 秒；删除优先；
W1–W4、W7 纳入 V10，W5/W6 保留显式产品决策 Gate。

Open questions:
none
```

Requirements Brief 于 2026-07-30 经用户确认；方案 A、四段设计和删除优先护栏
随后逐项确认。

## 3. Verified baseline and assumptions

| ID | Baseline evidence | Status |
| --- | --- | --- |
| B1 | targeted official report 为 `1/6` 正分，partial `0.08333333333333333` | verified |
| B2 | `container_network_*` 的分类顺序可稳定复现为错误的 `process` | verified |
| B3 | `Market/cloudbed-1:17` Top-100 中 memory 从第 21 名开始，packet-drop 在定向查询中存在 | verified |
| B4 | `Bank:107` 原始窗口存在 TCP wait、bandwidth 和 packet metric，但当前分类与 Top-20 均不能产生 `network latency` | verified |
| B5 | `Bank:16` 在目标 ±60 秒内存在 09:19 metric cause；`Market/cloudbed-1:10` 的两个目标附近均有 dependency observation | verified |
| B6 | production acceptance 已通过 deployment、dependency、traffic、healthy 四场景 | verified |
| B7 | `EvidenceItem.payload` 是 JSON 扩展点，SQLite schema V6 无需迁移即可保存 additive 字段 | verified |
| B8 | 当前工作树为 dirty，HEAD 不能唯一标识未来正式候选 | verified |
| A1 | 无 case 规则的共享语义可使六案例达到 `>=3/6` | unverified; targeted Gate |
| A2 | 七场景 lab 能验证受支持生产语义，但不能代表全部真实生产分布 | bounded claim |
| A3 | Prometheus `query_range` 在现有 lab 与受支持 Prometheus API 上可用 | unverified; contract + live Gate |

## 4. Risk obligations

| ID | Risk | Obligation | Requirement |
| --- | --- | --- | --- |
| O1 | Public/config compatibility | Prometheus query enum 只 additive；旧 API、默认配置和 Provider 开关不变 | R8 |
| O2 | Persisted compatibility | payload 只增字段；历史 Evidence、Review、Runtime、Replay、Diff 可读 | R7 |
| O3 | Partial/fallback | 空 optional series 与请求失败分开；range 失败显式标记 onset 不可用 | R5、R6 |
| O4 | Bounded data | 每个 range query 最多约 240 点；Provider 和 Top-N 上限保留 | R3、R5 |
| O5 | Evidence integrity | Hypothesis、Attribution、projection 只引用当前 Investigation 的有效 Evidence | R4、R9 |
| O6 | Ground Truth isolation | Signal Core、Provider、Analyzer、Projector 不读 record/report/score | R9 |
| O7 | Production safety | 所有生产读取只读；lab fault 不进入 Tool 面；不授予 NET_ADMIN | R10 |
| O8 | Answer leakage | 新 alert label/annotation 不包含 scenario、cause、reason 或 expected component | R10 |
| O9 | Source identity | 任何新 live Gate 前必须建立可复现 source identity | R12 |
| O10 | Complexity | 删除旧排序和重复 classifier；禁止第二套 taxonomy/onset/ranking | R11 |

## 5. Affected contract inventory

| Surface | Current source of truth | Consumers | Change | Compatibility |
| --- | --- | --- | --- | --- |
| Canonical metric semantics | Provider 私有字符串规则 | Analyzer、Attribution | 新内部 Signal Core | 不新增公共或持久化模型 |
| Evidence payload | `backend/domain/evidence.py` | persistence、API、Runtime、Replay | additive segment/onset/strength 字段 | 历史 payload 合法 |
| OpenRCA Metric/Dependency Provider | `backend/benchmarks/openrca/providers.py` | benchmark runner、tools | 复用 taxonomy、segment、balanced selection | 只影响新工件 |
| Prometheus Provider | `backend/providers/prometheus.py` | production orchestrator、tools | bounded `query_range` 和 optional network/process series | instant 兼容路径显式 partial |
| Prometheus metric enum | `backend/domain/tool_queries.py` | API/tool validation、Agent registry | additive network/process 名称；`metric_names` 上限与 enum 基数同步至 7 | 旧请求不变 |
| Attribution clustering | `backend/diagnosis/coordination_review.py` | Review、Runtime、Replay、OpenRCA | 显式 segment 不跨段合并 | 无 segment 的历史 Evidence 走旧聚类 |
| OpenRCA Projector | `backend/benchmarks/openrca/projection.py` | runner、targeted Gate | 删除二次归因/排序，只做字段投影 | metadata 保留安全状态 |
| Production artifact | `backend/services/production_acceptance.py` | gate runner、artifact validator | schema V2 七场景和 onset 字段 | schema V1 四场景继续读取 |
| Production lab/rules | lab service、Prometheus rules、Compose | acceptance runner | 新增三个 lab-only fault signal | 默认生产配置不变 |

## 6. Scope and non-goals

### 6.1 In scope

1. 一个共享内部 `signal_semantics.py`。
2. metric taxonomy、异常段、onset、归一化强度和 family-balanced selection。
3. OpenRCA metric/dependency 与生产 Prometheus adapter 集成。
4. 显式 anomaly segment 的 Attribution 兼容处理。
5. 删除失败 Projector 的 Evidence fallback 和第二套排序。
6. production acceptance schema V2 与三个新场景。
7. 双 targeted Gate、全量回归和可复现 source checkpoint。
8. source checkpoint 前完成 W1–W4、W7 全仓清理并重新运行 key-free regression。
9. 对 W5 model certification 和 W6 historical ReAct/LLM compatibility 形成明确
   retain/retire 决策；没有单独批准 retire 时保持兼容。

### 6.2 Non-goals

- 不增加规则注册器、DSL、factory、plugin、动态权重或配置文件；
- 不新增 vector store、模型服务、训练/校准流程或 LLM 判因；
- 不修改 SQLite schema、公共 Investigation API、Runtime 状态机或 Agent 权威边界；
- 不连接或写入真实生产环境；
- 不真实耗尽内存、杀死容器或修改容器网络；
- 不把六个已查看 Ground Truth 的案例称为无偏 holdout；
- 不宣称七个 lab 场景代表全部真实生产故障分布。

## 7. Architecture

### 7.1 Single shared module

新增 `backend/diagnosis/signal_semantics.py`，只允许：

- 最多两个内部 frozen dataclass：`SeriesPoint`、`AnomalySegment`；
- 三个公共纯函数：
  - `classify_metric_signal(metric_name)`；
  - `detect_anomaly_segments(baseline_points, observation_points)`；
  - `select_balanced_evidence(items, limit)`。

归一化强度、有限数校验和稳定 key 是模块内部 helper。不得拆成 taxonomy service、
detector class、selector class、registry 或配置层。

### 7.2 Data flow

```text
OpenRCA raw metric/trace ─┐
                          ├─ adapter + Signal Core ─ Evidence candidates
Prometheus query_range ───┘                         │
                                           balanced bounded selection
                                                    │
                                                    ▼
                                              EvidenceItem
                                                    │
                                                    ▼
                                      existing RcaAnalyzer
                                                    │
                                                    ▼
                              Hypothesis / CoordinationReview
                                                    │
                              ┌─────────────────────┴──────────┐
                              ▼                                ▼
                   production API/Runtime            thin OpenRCA projection
```

OpenRCA adapter 负责 vendor schema 和 component hierarchy；Prometheus Provider
负责 allowlisted read query；Signal Core 不知道 OpenRCA task、Ground Truth、生产
scenario 或 evaluator。

## 8. Signal contracts and algorithms

### 8.1 Canonical taxonomy

分类按以下固定顺序，不使用权重：

1. 网络上下文中：
   - `drop`、`dropped`、`error`、`loss`、`corrupt`、`retransmit`
     → `network_corruption`；
   - `latency`、`delay`、`rtt`、`wait` → `network_latency`；
   - `bytes`、`packets`、`bandwidth`、`throughput` → `traffic`。
2. 再识别 `memory`、`cpu`、`disk_io`、`process`、普通 `error`、普通 `latency`。
3. `container` 只表示作用域，不得单独推出 `process`。
4. 只有 dropped/error/loss/corrupt/retransmit packet 才是 corruption；普通 packet
   count 是 traffic。
5. 显式 Provider signal 必须属于现有 allowlist，否则保留为不可归因 Evidence，
   不能临时发明 reason。

### 8.2 Baseline and anomaly segment

1. baseline 是紧邻诊断窗口、与诊断窗口等长的前置窗口。
2. 每个 series 单独计算 median 和 MAD。
3. threshold 为 `max(3 * MAD, abs(median) * 0.1, epsilon)`。
4. deviation 是与本 series threshold 的比率，归一化强度限制在 `[0, 10]`。
5. 连续异常点属于同一 segment；出现正常点或相邻时间超过两倍中位采样间隔时
   断开。
6. 单点异常可以形成 segment，避免稀疏指标被强制丢失。
7. onset 是 segment 的第一个异常 observation，ended_at 是最后一个。
8. counter 类 PromQL 先转换为 rate/increase，再交给 Signal Core；Core 不猜测
   counter reset。

每个新 Evidence 至少包含：

```text
signal_type
signal_name
component
current_value
baseline_value
deviation_score          # 兼容字段，值已归一化到 0..10
normalized_strength
anomaly_onset
anomaly_ended_at
anomaly_segment_id
```

`EvidenceItem.timestamp` 等于 `anomaly_onset`。字段均为 additive。

### 8.3 Balanced bounded selection

1. 相同 `(signal_type, component, onset)` 的 adapter candidates 合并，保留原始
   signal names 和最强有限值。
2. 每个 signal family 内按
   `(-strength, onset, component, signal_name, stable_id)` 排序。
3. 先从每个实际出现的 family 取一个，再 round-robin 填满 limit。
4. family 数量不超过现有 canonical allowlist；若 limit 小于 family 数量，使用
   固定 allowlist 顺序，行为可重复且在 metadata 中标记 coverage truncation。
5. 不跨 family 比较原始 current value、change percent 或未封顶 deviation。

### 8.4 Component hierarchy and reason

OpenRCA adapter 可按公开 schema 拆分：

```text
node-6.checkoutservice-0
node = node-6
service = checkoutservice
instance = checkoutservice-0
component = node-6.checkoutservice-0
```

Bank 等无法可靠拆分的原子名称保持原样。Signal Core 不包含数据集名称或 case ID。

Attribution 继续使用现有 cause-aware component 优先级：

- network/resource/infrastructure 优先 node；
- application/deployment/dependency 优先 service/dependency；
- single-instance 优先 instance；
- 缺少层级时回退 component。

canonical reason 保持：

- `network_latency` → `network latency`；
- `network_corruption` → `network packet corruption`；
- `memory` → `container memory load`；
- `process` → `container process failure`；
- 其他现有映射不变。

### 8.5 Segment-aware Attribution

新 Evidence 有 `anomaly_segment_id` 时，同 component 不同 segment 不得合并。
Attribution occurred_at 使用 segment onset。历史 Evidence 没有 segment 字段时，
继续使用当前 `_time_clusters()`，保证历史 Replay 和 API 读取兼容。

## 9. Provider behavior

### 9.1 OpenRCA

- Metric Provider 使用共享 taxonomy、segment 和 balanced selection；
- Dependency Provider 保留 trace parent/child 聚合，但把 baseline、归一化强度、
  segment/onset 和 bounded selection 交给 Signal Core；
- Log Provider 只做已有 bounded 结构化映射，不增加 prompt 或答案读取；
- 不读取 `record.csv`、official report、score 或 task answer；
- task_index 不进入 Provider、Signal Core、Analyzer 或 Attribution。

### 9.2 Prometheus

Prometheus Provider 使用 `/api/v1/query_range`：

- query window 最多两小时；
- step 取不小于一秒且保证每 series 约不超过 240 点；
- qps、5xx、latency、cpu、memory 保留；
- network drops 与 process restarts additive；
- optional metric 查询成功但无 series 时表示“无该信号”，不是 Provider failure；
- HTTP、schema、非有限值或超限失败保持显式 partial/failed；
- range 失败可保留现有 instant Evidence，但必须设置
  `onset_unavailable=true`，不得伪造 segment；
- 新 production accuracy 场景不允许 onset fallback；
- `PrometheusQuery.metric_names` 上限与 `PrometheusMetric` enum 基数保持同步
  （新增后为 7），维持"单次工具调用可覆盖全部受支持信号"的现有语义；上限仍是
  固定 allowlist 大小，不放宽为有界集合之外的输入。

默认 Provider 开关、base URL、timeout、Agent 配置和只读 allowlist 不变。

## 10. Thin OpenRCA projection and mandatory deletion

Projector 新职责只有：

1. 校验 task_1–task_7 固定字段映射；
2. 只接收已持久化 authoritative root causes；
3. 丢弃无效 Evidence reference；
4. 按被评分字段稳定去重，同时保留 authoritative 顺序；
5. 截取 `expected_root_cause_count`；
6. 输出最小安全 audit。

Projector 不再：

- 调用 `build_root_cause_attributions()`；
- 读取 Hypotheses 后重建候选；
- 按 Provider diversity、specific signal 或 deviation 二次排序；
- 修改 root cause time；
- 用 Evidence fallback 生成新的 root cause。

audit 只保留：

```text
rule_version
scored_fields
selected_evidence_ids
projection_fallback
fallback_reason
projection_error
```

删除无人消费的 `input_candidate_count`、`valid_candidate_count` 和
`deduplicated_candidate_count`。若 authoritative root causes 不足，显式 fallback
并使 targeted Gate 失败。

## 11. Production seven-scenario Gate

### 11.1 Scenarios

保留：

| Scenario | Expected Top-1 |
| --- | --- |
| deployment_regression | deployment_regression |
| dependency_timeout | downstream_dependency_failure |
| traffic_spike | traffic_spike |
| healthy_control | UNKNOWN 或 confidence `<0.5` |

新增：

| Scenario | Cause | Component | Reason |
| --- | --- | --- | --- |
| memory_pressure | resource_saturation | checkout-service | container memory load |
| network_corruption | network_fault | checkout-service | network packet corruption |
| process_failure | process_or_container_failure | checkout-service | container process failure |

新场景通过 lab-only state 导出 bounded Prometheus metric，并产生通用请求失败。
fault endpoint 返回 `activated_at` 给 acceptance runner；该值不写入 alert、
Investigation、Evidence 或 DiagOps 配置。

所有故障场景必须通过 Prometheus rule → Alertmanager → DiagOps。label 与 annotation
只含 service、environment、severity 和通用 degradation 文本，不包含 scenario、
cause、reason、expected component 或 fault endpoint。

### 11.2 Artifact compatibility

- schema V1：历史四场景结构，继续可读和验证；
- schema V2：恰好七场景，新增 allowlisted component、reason、occurred_at、
  onset_error_seconds、onset_available；
- V2 三个新场景 onset error 必须 `<=60` 秒；
- V2 新场景不允许 `onset_unavailable`；
- 两版均要求 Evidence reference validity `100%`、read-only violation `0`、
  privacy scan 通过；
- 七场景总耗时保持 `<900` 秒。

该 Gate 只证明七个受控场景，不构成全部真实生产分布的准确率声明。

## 12. Error handling and safety

1. 单 series、单 query 或单 Provider 失败保持可见；其他 Evidence 继续。
2. 非有限值、反向窗口、超限点数或未知 taxonomy 不得生成高置信 cause。
3. Evidence/Hypothesis/Attribution reference 在持久化前继续验证。
4. optional series 缺失与查询失败使用不同状态。
5. Signal Core 不接受文件路径、URL、PromQL、task 或 scenario 参数。
6. DiagOps 只读 Prometheus 和 evidence volume；fault endpoint 只存在于 lab
   network，不注册为 Tool。
7. 不授予 NET_ADMIN，不执行真实内存耗尽或进程终止。

## 13. Whole-repository complexity audit and deletion contract

2026-07-30 的 `ponytail-audit` 覆盖整个仓库，而非只看 V10 diff。审查范围包括
`backend/`、`frontend/`、`tests/`、`scripts/`、根目录运行文档及 active routing；
历史 spec/plan 按仓库既有保留策略只检查引用关系，不把“文件很长”本身视为可删
依据。依赖导入核对没有发现可直接移除的 Python 或 frontend dependency。

V10 active path 的 mandatory finding：

| Finding | Location | Delete | Replacement | Status |
| --- | --- | --- | --- | --- |
| C1 | `projection.py:90-231` | Evidence fallback、canonical rebuild、第二套排序及其私有 helper | authoritative field dedup + Top-N | required |
| C2 | `projection.py:31-33` | 三个仅测试/文档消费的 candidate count | 最小 audit | required |
| C3 | `test_openrca_projection.py:62-164,231-258` | strongest/specific/provider-diversity/bounded-candidate 等失败启发式测试 | 保留 task mapping、field dedup、coherence、invalid-reference，并改为 thin projection contract | required |
| C4 | `openrca/providers.py:152-218,460+` | 私有 taxonomy 和全局 raw-deviation Top-N | Signal Core | required |
| C5 | `openrca/providers.py:339-389` | dependency 私有 threshold/未封顶排序 | 共享 segment/strength；保留 edge 聚合 | required |
| C6 | `coordination_review.py:295-334` | 不得整段删除，历史 Evidence 仍依赖 | 只作为 no-segment compatibility fallback | retained intentionally |
| C7 | `evaluator.py:324+` | targeted Gate 不是重复 release Gate | 保留开发 stop/go 语义 | retained intentionally |

全仓 finding 与处置边界：

| Finding | Scope | Finding | Disposition |
| --- | --- | --- | --- |
| W1 | `backend/diagnosis/context_store.py`、`tests/diagnosis/test_shared_context.py` | `SharedContextStore` 没有生产调用者，两个正式 repository 已直接持久化 context | 独立窄清理可直接删除，不是 V10 前置条件 |
| W2 | `SharedInvestigationContext`、`WorkbenchGraph*`、`QueryEvidenceProviderProtocol` | 定义没有生产消费者；现有测试只在证明对象能构造 | 独立窄清理删除定义及对应无效测试 |
| W3 | `tests/frontend/test_frontend_smoke.py`、`test_runtime_workbench.py` | 大量测试只断言源码包含字符串或固定实现片段，锁死重构但不验证页面行为 | 独立清理删除 source-shape assertions；保留可执行 selector、redaction、安全边界测试和 frontend build |
| W4 | `README.md`、`docs/superpowers/current.md` | 当前操作入口混入 V2–V9 年表和已冻结 dashboard，内容与历史 specs/plans 重复 | 独立文档清理；保留当前运行命令、安全边界、active pointers 和冻结 artifact 索引 |
| W5 | `backend/services/v7_live_acceptance.py` 及其测试 | 仅 CLI/README/测试消费，但仍是现有 Provider/model certification artifact 的生产者 | 需先确认是否终止 model certification；本规格不删除 |
| W6 | historical ReAct/LLM read models、SQLite rows 和 read-only API | 新流程不再写入，但明确承担历史数据库读取兼容 | 保留；只有迁移/保留期决策批准后才可删除 |
| W7 | repository `getattr`/optional-method fallback | 两个正式 repository 已实现这些方法，部分 fallback 只服务不完整 test double | 后续窄清理改为直接调用并同步最小 test double；不引入新 Protocol 层 |

V10 复杂度验收：

- 只能新增一个生产模块；
- 最多两个内部 dataclass、三个公共纯函数；
- 不存在两套 taxonomy、onset 或 candidate ranking；
- Runner/Evaluator 只接线，不新增诊断算法；
- 不新增 dependency；
- 历史 docs/artifacts 保留，但从 active routing 移除；
- milestone review 必须先验证 C1–C5 已删除，再接受新增代码。

W1、W2 作为 V10 M0，在 Signal Core 前删除；W3、W4、W7 作为 V10 M4.5，在全量
key-free regression 和 source checkpoint 前完成。这样最终 production/OpenRCA
Gate 与最终待发布源码一致，不用在清理后重跑 live Gate。

W5、W6 作为同一 M4.5 的产品决策检查。没有书面 retire 决策时必须保留现有能力，
并记录保留原因；不得把“代码旧”当作删除认证生产者或历史持久化读取合同的授权。

预计 V10 共享实现前可先删除约 100 行生产逻辑。LOC 不是独立成功指标，但新增
抽象若未替换旧路径即为 blocking finding。

## 14. Verification and stop/go

### 14.1 Focused checks

1. taxonomy precedence：container-network、dropped packet、plain packet、
   TCP wait、memory、process；
2. segment：baseline、零 baseline、非有限值、单点、正常点断段、采样 gap、
   onset/ended_at、强度封顶；
3. balanced selection：family coverage、dedup、limit、repeatability；
4. OpenRCA hierarchy、Metric/Dependency segment、bounded degradation；
5. Prometheus range parse、240 点上限、empty optional、partial/fallback；
6. Analyzer network/resource/process、UNKNOWN、stable order；
7. Attribution segment isolation 与历史聚类；
8. thin projection、invalid reference、task mapping、audit；
9. production artifact V1/V2、answer-leak scan、七场景校验；
10. Runtime/Replay/Diff 和 API compatibility。
11. cleanup caller scan、frontend executable behavior、repository direct-call 和
    README/current routing。

### 14.2 Execution order

1. M0 删除 W1/W2 并完成 focused checks；
2. TDD 实现 Signal Core、Provider、Attribution、Projector 和七场景 Gate；
3. M4.5 删除 W3/W4/W7，记录 W5/W6 retain/retire disposition；
4. 运行 focused suites、Ruff、全量 pytest、frontend build 和 Runtime acceptance；
5. 建立 source identity checkpoint；若需要 commit，先取得单独 Git 授权；
6. 在同一可复现 source 上先跑 production 七场景 Gate；
7. production Gate 通过后跑冻结六案例；
8. 六案例达到 `>=3/6` 且 time/reason/component 各至少一个正分后，才允许在同一
   source 上运行正式 40-case。

### 14.3 Gate thresholds

Production V2：

- `7/7`；
- 原四场景不回归；
- 新三场景 cause/component/reason 正确；
- 新三场景 onset error `<=60s`；
- Evidence validity `100%`；
- read-only violation `0`；
- privacy scan 通过；
- total duration `<900s`。

OpenRCA targeted：

- `6/6` completed；
- 至少 `3/6` official rows score `>0`；
- task_1、task_2、task_3 各至少一个 score `>0`；
- Evidence validity `100%`；
- projection error/fallback `0`；
- read-only violation `0`。

正式 40-case 继续使用原 V10 release threshold，包括 official partial
`>=0.10`、time/reason/component 均 `>0`、cardinality `100%`、nonempty
`>=95%`、Evidence `100%`、read-only `0` 和 deterministic token/cost `0`。

### 14.4 Stop rules

- focused checks 通过但 source identity 不可复现：停止并请求 Git 授权；
- cleanup 删除了仍有 caller、公共 API 或历史持久化合同：恢复并退回 review；
- production Gate 失败：只修已证明的共享根因，不启动 OpenRCA 实跑；
- 六案例失败：记录证据并停止，不调权重、不加 case 规则、不跑 40-case；
- 正式 Gate 失败：保留工件，不宣称 V10 或生产准确率提升。

## 15. Requirements and traceability

| requirement_id | Behavior | Baseline evidence | Acceptance check | Plan task | Focused check | Final gate | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | OpenRCA 与 Prometheus 共用固定 taxonomy | B2–B4 | precedence cases | T1–T3 | Signal Core/Provider tests | 双 targeted Gate | approved |
| R2 | series 产生 bounded segment、onset 和 0..10 strength | B3–B5 | segment cases | T1–T3 | Signal Core/Provider tests | onset audits | approved |
| R3 | Top-N 保证 signal-family coverage 且确定 | B3、B4 | coverage/repeatability | T1–T3 | selector/Provider tests | artifact metadata | approved |
| R4 | Analyzer/Attribution 使用 segment 且引用有效 | B5、B7 | segment isolation/reference | T4 | Analyzer/coordination tests | Evidence 100% | approved |
| R5 | Prometheus range bounded，optional empty 与 failure 分离 | B6、A3 | query range/degradation | T3 | Prometheus tests | production Gate | approved |
| R6 | onset fallback 显式且新场景禁止 fallback | A3 | range failure cases | T3、T5 | Provider/gate tests | production Gate | approved |
| R7 | schema V6、历史 Evidence/Runtime/Replay/API 兼容 | B7 | historical fixtures | T2–T5 | replay/API/artifact tests | full regression | approved |
| R8 | enum additive、默认配置与旧请求不变 | B7 | old request/config snapshots | T3 | domain/config tests | full regression | approved |
| R9 | Projector 只做字段投影，不读 GT、不二次归因 | B1、B8 | caller/privacy audit | T4 | projection tests | OpenRCA Gate | approved |
| R10 | 七场景无答案泄漏、只读、安全且 `<900s` | B6、O7、O8 | artifact/config scan | T5、T8 | lab/acceptance tests | production Gate | approved |
| R11 | 删除 C1–C5，禁止重复框架和第二套排序 | complexity audit | diff/caller review | T2、T4 | deletion audit | milestone review | approved |
| R12 | 所有 live Gate 使用同一可复现 source，且 targeted 通过后才跑 40-case | B8 | release preflight | T7–T10 | source/Gate checks | source checkpoint | approved |
| R13 | 生产七场景与 OpenRCA targeted 达到确认阈值 | B1、B6 | live evaluations | T8–T10 | focused gates | 双 targeted Gate | blocked（生产 7/7 verified；OpenRCA targeted 2/6 未达 3/6，用户 2026-07-31 接受并归档，T10 按停止规则未执行） |
| R14 | 声明仅覆盖实际 Gate，不夸大生产泛化 | A2 | documentation audit | T5、T8–T10 | wording scan | final report | approved |
| R15 | source checkpoint 前删除 W1–W4、W7，保留真实行为测试并保持公共/持久化合同 | whole-repo audit | caller scan、focused/full regression | CL0、CL1 | cleanup focused checks | final source audit | approved |
| R16 | W5/W6 只有书面 retire 决策才删除，否则记录并保留认证与历史读取能力 | whole-repo audit | capability/retention decision | CL2 | decision ledger、compatibility tests | final source audit | approved |

## 16. Review ledger

| finding_id | origin | severity | Root cause | Disposition | Resolution | Regression check | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | new_evidence | high | container token 抢先把 network metric 分类为 process | accepted | 网络语义先于 container scope | precedence test | open |
| F2 | new_evidence | high | raw deviation 跨 series 不可比且零 baseline 爆炸 | accepted | 每 series threshold、0..10 strength | zero-baseline test | open |
| F3 | new_evidence | high | 全局 Top-20 丢失 memory/network family | accepted | family-first round-robin | coverage test | open |
| F4 | new_evidence | high | 峰值替换和跨段聚类丢失真实 onset | accepted | segment onset + explicit segment boundary | onset test | open |
| F5 | scope_change | high | 四场景 production Gate 不能证明新语义 | accepted | schema V2 七场景 | V1/V2 + live gate | open |
| F6 | scope_change | high | 新 acceptance 改变已批准规格与计划 | accepted | 新 spec/plan 重新审批 | current.md status | open |
| F7 | missed | high | range fallback 若静默会伪造 onset | accepted | explicit onset_unavailable；新场景禁止 | failure test | open |
| F8 | missed | high | lab scenario 名称可能泄漏答案 | accepted | generic alert + persisted event scan | leak test | open |
| F9 | missed | high | source dirty 时 live result 不可复现 | accepted | 所有 live Gate 前建立 source checkpoint | preflight | open |
| F10 | new_evidence | high | 继续叠加 Projector 会形成第二套 Analyzer | accepted | C1–C5 mandatory deletion | diff/caller audit | open |
| F11 | missed | medium | 普通 packet count 被 corruption 规则误伤 | accepted | corruption 必须含 drop/error/loss/corrupt/retransmit | taxonomy test | open |
| F12 | missed | medium | 删除历史 time clustering 会破坏旧 Evidence | accepted | no-segment compatibility fallback 保留 | historical replay | open |
| F13 | missed | medium | plan 最初用 README 间接引用正式 40-case 命令，不满足 exact final Gate command | accepted | 在 T10 内联 deterministic prepare/run/evaluate、official evaluator 和 gate 命令 | cold plan reread | resolved |
| F14 | scope_change | high | 用户要求把全仓清理写入 V10 plan，原合同明确排除 W1–W7 | accepted | 增加 M0/M4.5、R15/R16，并把全部清理放在 source checkpoint 前 | amended spec/plan cold review | resolved |
| F15 | missed | high | 整文件删除 Runtime frontend source-shape tests 会同时丢失 projection 行为覆盖 | accepted | 删除实现形状断言，但保留最小 executable runtime projection tests | amended plan cold review | resolved |
| F16 | missed | low | plan 清理任务 C0–C2 与 spec mandatory findings C1–C7 编号冲突，单任务执行者会混淆 | accepted | plan 任务改名 CL0–CL2，traceability plan task 列同步 | cold plan reread | resolved |
| F17 | missed | low | spec "约 100 行生产逻辑" 与 plan "净删除约 200 行" 口径不一致 | accepted | plan CL0 注明净删除含测试、生产代码约 100 行的口径 | cold plan reread | resolved |
| F18 | missed | low | C5 行范围 339-383 未覆盖 :389 的 dependency 排序与截取 | accepted | 行范围修正为 339-389，执行以函数边界为准 | provider deletion diff | resolved |
| F19 | missed | medium | `PrometheusQuery.metric_names` max_length=5 与新增后 7 值 enum 的覆盖关系未明确，会破坏"单调用覆盖全部受支持信号"的现有语义 | accepted | 上限与 enum 基数同步至 7，仍为固定 allowlist 大小 | domain/tool tests | resolved |
| A1-F1 | new_evidence | high | Amendment A1"时间投影永不锚定不可辨识 onset"未指定实现机制，仅改 attribution 排序无法实现 | accepted | 用户决定撤回 Amendment A1，不改代码；机制分析归档于 `docs/superpowers/openrca-v10-6-case-failure-analysis.md` §6 | cold review report | resolved |
| A1-F2 | new_evidence | high | 二元 edge-onset 规则非 baseline-aware，与 V2 Gate `onset_unavailable` 消费语义相撞；baseline-aware 修正后预备数据 baseline 完整，规则对失败案例不起作用 | accepted | 随 Amendment A1 撤回；证明记录归档于失败分析 §6 | cold review report | resolved |
| A1-F3 | new_evidence | medium | strength 饱和（threshold=ε 时 clamp 10）本身未修，R17/R18 不改变 Bank 失败路径 | accepted | 书面承认为已诊断未修复项，归档于失败分析 §6.1 | failure analysis doc | resolved |
| A1-F4 | new_evidence | medium | OpenRCA dependency error 路径手工构造 segment，绕过 Signal Core 拿不到 onset 可辨识性 | accepted | 随 Amendment A1 撤回，无代码影响；记录为未来修复面 | cold review report | resolved |
| A1-F5 | new_evidence | medium | R17 差分转换的排序/Δt=0/跨窗边界未定义 | accepted | 随 Amendment A1 撤回；边界条件清单保留在失败分析 §6.1 供复用 | cold review report | resolved |
| A1-F6 | new_evidence | low | 历史 payload 缺失 `onset_unavailable` 的默认平局语义未成文 | accepted | 随 Amendment A1 撤回，无行为变化 | cold review report | resolved |
| A1-F7 | new_evidence | low | "与生产 counter→rate 同构"措辞 overstated（生产含 reset 处理与外推） | accepted | 撤回后无措辞残留 | cold review report | resolved |
| A1-F8 | new_evidence | low | T11 测试清单与文件 ownership 缺口 | accepted | 随 Amendment A1 撤回 | cold review report | resolved |

## 17. Alternatives

### A. Shared pure-function Signal Core

采用。一个内部模块替换重复 Provider 和 Projector 逻辑，不改变持久化模型。

### B. Persist raw series and normalize in Analyzer

拒绝。会放大 Evidence payload、SQLite、Runtime、Replay 和 API 风险。

### C. Patch OpenRCA and Prometheus independently

拒绝。改动短但保留两套 taxonomy/onset/ranking，不满足共享生产语义。

## 18. Approval state

本规格曾于 2026-07-30 获用户批准；用户随后要求把全仓清理纳入 V10，触发 F14
scope change 后重新进入 `review_required`。第二轮独立冷审的 F16–F19 全部
resolved 后，用户于 2026-07-30 书面批准当前 amended 内容，状态回到 `approved`：

- implementation plan 同步获批；
- 执行按 plan milestone 顺序进行，T7 source checkpoint 前需单独取得 Git 授权；
- 后续证据若改变目标、范围、行为、公共或持久化合同、安全边界、迁移或验收标准，
  本规格与 plan 同时退回 `review_required`。

## 19. Amendment A1（2026-07-31）：counter 语义与 onset 可辨识性 —— **已撤回**

触发：T9 frozen six-case targeted Gate 真实运行失败（2/6 正分 < 3/6，time 0），
根因分析确认两个与 Ground Truth 无关的客观语义缺陷。本增补曾追加两条健壮性
要求；独立冷审（findings A1-F1…A1-F8，见 §16）证明其核心机制不成立：

- R18 的"永不锚定"仅靠 attribution 排序无法实现（A1-F1），且二元 edge 规则
  非 baseline-aware、与 V2 Gate `onset_unavailable` 消费语义相撞；修正为
  baseline-aware 后，因预备数据 baseline 窗口完整，规则对失败案例不起作用
  （A1-F2）。
- R17 客观正确但不改变 Bank 失败路径；strength 饱和未被触及（A1-F3）。

用户于 2026-07-31 决定**撤回 Amendment A1（R17/R18 不实现），接受 T9 结果
并归档**。原批准合同（§1–§18）保持 `approved` 不变；诊断结论与已诊断未修复
项归档于 `docs/superpowers/openrca-v10-6-case-failure-analysis.md`。以下
§19.1–§19.5 保留为撤回方案的历史记录，不再具有执行效力。

### 19.1 诊断证据（仅来自 run 输出与原始遥测，未使用 Ground Truth 做设计）

1. `metric_container.csv` 含真累计 counter（如 `total_commands_processed`、
   `total_connections_received`、`keyspace_hits`，绝对值达 4.8e9）。OpenRCA
   adapter 未做 counter→rate 转换，把累计值当 gauge：绝对水平远超 baseline
   即产生 strength 10 假异常（"395546943 vs 83"）。`metric_app.csv` 的
   rr/sr/cnt/mrt 是每分钟聚合率，不受此影响。
2. 准零序列（baseline 恒 0、MAD=0、threshold=ε）下任何单点 blip 都被 clamp
   成 strength 10，strength 失去排名区分度，attribution 退化为 onset 升序
   tie-break，而窗口首样本段（edge artifact）系统性赢得平局。
3. 钉在窗口首个 observation 样本的段无法区分"窗口内新故障"与"窗口前已存在
   的状况"——其真实 onset 不可观测。这与生产侧已批准的 `onset_unavailable`
   （range 数据不足时显式降级）是同构语义。

### 19.2 R17：OpenRCA adapter counter→rate

对 OpenRCA metric adapter 的每个 `(component, instance, metric)` series：
若 observation+baseline 合并序列**严格单调不减且至少两次严格递增**，判定为
累计 counter，先转换为相邻样本差分率（rate = Δvalue/Δt），再交给 Signal
Core；其余 series 语义不变。这是遥测 schema 层语义修正，与生产 adapter 的
counter→rate 模板同构，不读取任何 metric 名称黑名单、task 或案例标识。

- 已知的可接受取舍：counter reset（窗口内值下降）使序列非单调 → 按 gauge
  处理，记录为非目标；单调上升的 gauge（如缓慢泄漏）被转换为增长率，其
  阶跃仍会被检测（baseline rate≈0 vs observation rate>0），语义可辩护。

### 19.3 R18：onset 可辨识性与 attribution 平局裁决

`detect_anomaly_segments` 为每个 segment 增加 onset 可辨识性：若 segment
onset 等于窗口内首个 observation 样本时间戳（窗口内没有任何更早样本），则
`onset_identifiable=False`。adapter 把它映射为既有 payload 字段
`onset_unavailable`（additive，历史 payload 不变；证据本身保留、不删除）。

Attribution 排序契约改为：cluster score 不变；同分时，onset 可辨识的
cluster 优先于不可辨识的，其后才按 occurred_at、component、reason 平局裁决。
时间投影永不锚定不可辨识 onset；全部 cluster 不可辨识时走 Projector v2 既有
显式 fallback。这与 R6 的"onset fallback 显式"一致。

### 19.4 兼容性与验证

- 公共/持久化合同：`AnomalySegment` 增加只读字段；Evidence payload 仅 additive
  复用 `onset_unavailable`；无 API、DB schema、artifact schema 变化。
- 验证：聚焦测试（counter 转换、edge onset、平局裁决、历史 payload 兼容）+
  Ruff + 全套件 + **T8 重跑不得回归** + **T9 重跑一次定结果**。
- 停止规则：T9 重跑无论通过与否都结束本轮迭代；不再进行第二次修订或调参，
  未通过则按 V9 先例写失败分析归档。T10 仍仅在 T9 通过后执行。

### 19.5 新增需求追踪

| ID | Requirement | Baseline | Acceptance | Plan task | Focused check | Final gate | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R17 | ~~OpenRCA adapter 单调累计 series 先转 rate 再进 Signal Core~~（随 Amendment A1 撤回，不实现；作为已诊断缺陷归档） | 19.1-1 | — | — | — | — | withdrawn |
| R18 | ~~edge onset 不可辨识、additive `onset_unavailable`、attribution 同分可辨识优先~~（随 Amendment A1 撤回，不实现；冷审证明机制无效） | 19.1-2/3 | — | — | — | — | withdrawn |
