# V9 OpenRCA 6-Case 失败分析

日期：2026-07-29  
状态：完成（只读分析，不修改 V9 代码与冻结工件）

## 1. 结论

V9 的主要问题不是 Adaptive 工具预算不足，而是 Fixed 与 Adaptive 共用的
“异常信号 → 根因声明 → 评测输出”契约不适合 OpenRCA：

1. Metric Provider 将每个阈值异常直接写入 `root_cause_claims`，没有候选排序、
   去重、时间聚类或因果归因。
2. 最终合成被要求从 `root_cause_claims` 原样复制 component、reason 和 time，
   因而不能把底层 metric 名归纳为 OpenRCA 需要的故障原因。
3. OpenRCA 问题明确给出单故障或双故障，但输出没有根因数量约束。40-case 中，
   Fixed 仅 `9/40`、Adaptive 仅 `8/40` 与期望数量一致。
4. Adaptive 仍使用相同 Provider 和最终合成契约。它能增加或缩小信号集合，
   但没有增加区分正确根因的机制。

六个案例覆盖 time、reason、component 三类任务。Fixed 与 Adaptive 在六个案例
上均为 `0` 分；其中三组配对预测完全相同，其余三组虽改变候选数量，但没有
改善得分。

**决策建议**：V9 保持冻结，Fixed 作为诊断质量基线；在设计新的 Adaptive
策略前，先修复共享的候选语义、排序和数量契约。

## 2. 数据来源与边界

### 2.1 冻结工件

分析对象：

```text
D:\data\OpenRCA\results-v9-final-9018f99-merged-audited\
  run-merged-20260728T131350272103Z
```

| 工件 | SHA-256 |
| --- | --- |
| `run-manifest.json` | `66AB4BD47C80027AE0037990EAE17DBDF89833EEF4A97593EED4A897ACF33213` |
| `summary.json` | `52B43BD7196C76DBFFDCC65647D49C5E2FDAA60332E493AD8E5B4A80D9986F87` |
| `fixed-predictions.csv` | `0A10B5329ED4577DF0DD92B0C8DCAD720A5137C65ED9EAB472971EFC4BACFA1A` |
| `adaptive-predictions.csv` | `F3C597DA723D808B83C23B5A246397964AD6E1C8593B01B7D776B1139FFA1DCE` |
| `official-report.csv` | `695894AA795FF66A25145684C33B09D60E59A45D7B40B72935F82B73AD5D158C` |

正式评测对应 commit：
`9018f990d6088ca7fb9efb097d1233693fee1cf2`。

### 2.2 分析范围

- 从冻结的 40-case 中选取 time、reason、component 各两个案例。
- 每个案例比较同一输入下的 Fixed 与 Adaptive 正式预测。
- Ground Truth 只用于事后评分与归因，不进入诊断输入。
- 结合当前代码静态追踪预测生成路径。
- 不重新调用模型，不修改 Runtime 数据库，不覆盖任何冻结工件。

以下正式失败不纳入六个语义案例，避免把运行可用性与诊断准确性混为一谈：

- Fixed：`Market/cloudbed-2:70`，Agents runtime timeout。
- Adaptive：`Market/cloudbed-2:62`、`Market/cloudbed-2:48`，Agents runtime
  timeout。
- Adaptive：`Market/cloudbed-2:70`，budget exhausted。

## 3. 总体数据

| 指标 | Fixed | Adaptive | 判断 |
| --- | ---: | ---: | --- |
| strict accuracy | 0 | 0 | Adaptive 无准确率收益 |
| partial score | 0 | 0 | 任一目标字段均未命中 |
| 非空预测 | 39/40 | 37/40 | Adaptive 更低 |
| Evidence 引用有效率 | 100% | 100% | V9 证据完整性目标已实现 |
| 平均 Tool Calls | 4.00 | 14.85 | Adaptive 为 Fixed 的 3.71 倍 |
| Duplicate Query Rejections | 0 | 88 | Adaptive 存在明显无效查询 |
| 平均耗时 | 336,843 ms | 522,245 ms | Adaptive 高 55.0% |
| 估算成本 | 5.76109 | 8.525555 | Adaptive 高 48.0% |
| 平均输出根因数 | 8.18 | 7.98 | 两者都未收敛到 1–2 个根因 |
| 根因数量匹配 | 9/40 | 8/40 | Adaptive 未改善数量契约 |

根因数量匹配的任务分布：

| 任务 | 案例数 | Fixed | Adaptive |
| --- | ---: | ---: | ---: |
| `task_1`：time | 16 | 2/16 | 5/16 |
| `task_2`：reason | 15 | 5/15 | 2/15 |
| `task_3`：component | 9 | 2/9 | 1/9 |

## 4. 六个案例

### 4.1 Time：`Bank:16`

问题要求：在 `09:00–09:30` 内定位一个根因时间。  
Ground Truth：`2021-03-09 09:18:00`，容差 ±1 分钟。

| 策略 | 输出 |
| --- | --- |
| Fixed | 20 个根因，时间全部为 `09:00:00` |
| Adaptive | 与 Fixed 完全相同：20 个根因，时间全部为 `09:00:00` |

Fixed/Adaptive 都比目标早 18 分钟。20 个输出来自 Mysql、IG、Redis、
Tomcat、MG 等多个组件在窗口起点的异常，没有时间聚类或根因数量收敛。

**失败位置**：Metric 异常排序和截断。Provider 按 timestamp 升序排列异常后
直接取前 `limit` 个，窗口起点的大量异常占满候选。

### 4.2 Time：`Market/cloudbed-1:10`

问题要求：在 `13:30–14:00` 内定位两个根因时间。  
Ground Truth：

1. `2022-03-20 13:39:48`，容差 ±1 分钟。
2. `2022-03-20 13:51:51`，容差 ±1 分钟。

| 策略 | 输出 |
| --- | --- |
| Fixed | 20 个根因，时间全部为 `13:30:00` |
| Adaptive | 与 Fixed 完全相同：20 个根因，时间全部为 `13:30:00` |

输出分别比两个目标早 9 分 48 秒和 21 分 51 秒。前 16 个候选主要是多个 Pod
的 `container_network_transmit_packets.eth0`，随后才出现 CPU 指标。

**失败位置**：时间顺序代替异常重要性排序；同一时刻、同一指标在多个组件上的
异常没有聚合，导致前 20 个名额被窗口起点信号占满。

### 4.3 Reason：`Bank:107`

问题要求：定位一个根因原因。  
Ground Truth：`network latency`。

| 策略 | 输出 |
| --- | --- |
| Fixed | 20 个 metric anomaly，涉及 disk、CPU、process 和 network 指标 |
| Adaptive | 2 个 metric anomaly：disk busy 和 CPU I/O wait |

Adaptive 将 20 个候选缩小到 2 个，但两者都没有输出 `network latency`。
Fixed 中虽然出现 network 指标，最终结果仍保留底层 metric 名，没有完成从
观测信号到故障语义的归纳。

**失败位置**：reason 抽象层。Provider 只产生
`metric anomaly: <metric_name>`，最终合成又必须原样复制，正确原因在当前
契约下不可表达。

### 4.4 Reason：`Market/cloudbed-1:17`

问题要求：定位两个根因原因。  
Ground Truth：

1. `container memory load`
2. `container network packet corruption`

| 策略 | 输出 |
| --- | --- |
| Fixed | 1 个：`metric anomaly: container_cpu_cfs_throttled_periods` |
| Adaptive | 与 Fixed 完全相同 |

两种策略既没有匹配两个故障的数量，也没有匹配 memory 或 network packet
语义。

**失败位置**：候选覆盖与 reason 归纳同时失败。Adaptive 没有识别到明确的
证据缺口，也没有通过后续工具调用补齐缺失信号。

### 4.5 Component：`Bank:12`

问题要求：定位两个根因组件。  
Ground Truth：`MG01`、`Tomcat04`。

| 策略 | 输出 |
| --- | --- |
| Fixed | 1 个：`Tomcat03` |
| Adaptive | 20 个，包含 Tomcat03、apache、MG02、IG02、Redis、Mysql 等 |

Adaptive 扩大了候选集合，但没有包含 `MG01` 或 `Tomcat04`，并将一个错误
候选扩展为 20 个错误候选。

**失败位置**：组件排序。系统没有把跨指标、跨组件的异常聚合为组件级分数，
也没有利用题目给出的“双故障”约束停止扩张。

### 4.6 Component：`Market/cloudbed-2:52`

问题要求：定位两个根因组件。  
Ground Truth：`checkoutservice`、`node-6`。

| 策略 | 输出 |
| --- | --- |
| Fixed | 3 个，component 均为 `node-6.currencyservice2-0` |
| Adaptive | 7 个，component 均为 `node-6.currencyservice2-0` |

同一组件因不同 metric 被重复输出。预测既没有归一化到 `node-6`，也没有找到
`checkoutservice`。

**失败位置**：组件粒度和去重。当前输出单位实际是
`component × metric anomaly`，而评分单位是故障组件。

## 5. 根因链路

```mermaid
flowchart LR
    A["当天 Telemetry 全量扫描"] --> B["MAD 阈值异常"]
    B --> C["按时间升序截取前 limit 个"]
    C --> D["每个异常直接成为 root_cause_claim"]
    D --> E["Coordinator 原样复制三元组"]
    E --> F["按时间排序并输出全部 RootCause"]
    F --> G["OpenRCA 按指定字段和故障数量评分"]
```

### 5.1 原始异常被提升为根因

`OpenRcaMetricProvider.collect()` 对当天同组件、实例和指标建立 median/MAD
基线，把超过阈值的每个 point 作为 anomaly。随后：

```python
anomalies.sort(key=lambda item: str(item["timestamp"]))
anomalies = anomalies[:limit]
```

这些 anomaly 又被逐项转换为：

```text
component = 原组件
reason = "metric anomaly: <metric_name>"
occurred_at = 原时间
```

并写入 `root_cause_claims`。这里缺少“异常候选”和“已归因根因”之间的语义
边界。

代码位置：

- `backend/benchmarks/openrca/providers.py:133`
- `backend/benchmarks/openrca/providers.py:204`
- `backend/benchmarks/openrca/providers.py:234`

### 5.2 最终合成没有重新归因的权限

`_synthesis_prompt()` 明确要求：

- 存在 `root_cause_claims` 时至少返回一个根因。
- component、reason、occurred_at 必须从 claim 原样复制。
- 不允许根据其他证据生成新的根因三元组。

这一约束保证了 Evidence 引用完整性，但也把 Provider 的异常检测结果变成了
最终答案上限。Evidence 有效不等于诊断正确。

代码位置：`backend/diagnosis/agents_runtime.py:2175`。

### 5.3 任务和数量约束没有进入最终输出契约

OpenRCA 的 `task_index` 被写入 Prediction CSV，但 `_synthesis_prompt()` 不接收
event 或 task，`_official_prediction()` 也只是把全部 RootCause 按时间排序后
序列化为完整三元组。

代码位置：

- `backend/benchmarks/openrca/runner.py:389`
- `backend/diagnosis/agents_runtime.py:2175`
- `backend/benchmarks/openrca/runner.py:452`

题目中的“一个/两个 failure”只存在于 Incident description 的自然语言中，没有
成为可验证的根因数量约束。

### 5.4 Adaptive 只能重复共享缺陷

Adaptive Tool Session 可以扩大、过滤或重试只读查询，但返回结果仍经过同一
Provider claim 和 Coordinator exact-copy 契约。其新增调用主要改变候选数量，
没有增加以下能力：

- 异常严重度排名。
- 时间变化点与异常簇检测。
- 组件级聚合和归一化。
- metric 到故障原因的受控语义映射。
- 按问题所述故障数量停止。

所以 Adaptive 在修复共享基线前没有独立优化价值。

## 6. 失败分类

| ID | 失败类型 | 覆盖案例 | 证据 |
| --- | --- | --- | --- |
| F1 | anomaly 与 root cause 语义混同 | 6/6 | 所有输出均直接使用 Provider claim |
| F2 | 根因数量契约缺失 | 6/6 | 六组 Fixed/Adaptive 均未匹配期望数量 |
| F3 | 时间按窗口起点塌缩 | 2/2 time | 两组预测全部落在窗口起点 |
| F4 | reason 抽象层缺失 | 2/2 reason | 输出 metric 名，目标为故障语义 |
| F5 | component 聚合、归一化和排序缺失 | 2/2 component | 候选扩张或同组件重复 |
| F6 | Adaptive 增加调用但不增加判别能力 | 6/6 | 六组得分均未超过 Fixed |

## 7. 后续修复假设与 Gate

本报告不批准实现；以下是假设，需进入新的迭代设计。

### 7.1 优先验证的最小假设

1. **候选语义分层**：原始异常改为 `candidate_signals`；只有经过明确聚合和
   归因的结果才能成为 `root_cause_claims`。
2. **通用排序与去重**：按稳健异常强度、时间簇、组件和依赖关系排序；同组件
   的多个 metric 不重复占用根因名额。
3. **数量约束**：从安全的 Benchmark Case 元数据取得期望故障数量，输出
   Top-N；不要让模型自行猜测数量。
4. **受控原因归纳**：使用通用、可审计的 metric → failure category 规则或
   结构化推断，禁止硬编码这 40 个 case 的 Ground Truth。
5. **Fixed 先行**：先证明共享基线能得分，再评估 Adaptive 是否能补齐明确的
   Evidence gap。

### 7.2 开发集 Gate

这六个案例已被查看 Ground Truth，只能作为开发和回归样本，不能再作为无偏
效果证明。

建议最小 Gate：

- 六个案例的目标字段 partial score 必须从 0 提升。
- 六个案例的输出根因数量必须与题目一致。
- time 输出不得统一塌缩到窗口起点。
- component 输出按评分粒度去重。
- Evidence 引用有效率继续保持 100%。
- 只读违规继续保持 0。

### 7.3 新 Holdout Gate

在写实现前，从未用于本次分析的 OpenRCA case 中冻结新的 holdout：

- 固定 case manifest、模型、prompt、预算、价格和 evaluator commit。
- Ground Truth 在预测冻结前继续隔离。
- Fixed 作为第一条质量基线。
- Adaptive 只有在 holdout partial score 高于 Fixed，且非空率不低于 95%、
  Evidence 引用 100% 有效、只读违规为 0 时，才进入默认策略候选。
- 如 Adaptive 无分数收益，保留为实验模式，不用增加预算掩盖问题。

## 8. 明确不做

- 不通过增加 Agent 数量、Tool budget、timeout 或候选 `limit` 修复零分。
- 不把 OpenRCA Ground Truth、case ID 或固定答案写入诊断逻辑。
- 不牺牲 Evidence 引用校验换取模型自由生成。
- 不重跑已经暴露 Ground Truth 的 40-case 后声称得到新的无偏成绩。
- 不在共享 Fixed 基线仍为 0 时单独优化 Adaptive。

## 9. 下一步

如果继续提分，应启动一个新的 Full 级诊断质量迭代，先确认
`candidate_signals` 与 `root_cause_claims` 的契约、通用排序方法和新 holdout
协议，再写 spec 和 plan。

如果不继续提分，V9 当前结果已经足以支持一个诚实结论：系统的 Runtime、
Evidence 完整性和审计能力成立，但 OpenRCA 未证明诊断准确性，Adaptive 在
当前实现中不值得额外成本。
