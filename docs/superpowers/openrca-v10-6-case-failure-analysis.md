# V10 OpenRCA 6-Case 失败分析

日期：2026-07-31
状态：完成（只读分析，不修改 V10 代码与冻结工件）

## 1. 结论

V10 把 V9 的"异常直接提升为根因"契约替换为共享 Signal Core、segment-aware
Attribution 与 thin Projector 后，诊断质量从全零提升到 2/6 正分
（component 0.25、reason 0.33），基础设施目标全部保持（6/6 completed、
Evidence 引用 100%、projection error/fallback 0、只读违规 0、零 Model
token），但 targeted Gate 仍未达到 ≥3/6 且 time 全零。失败根因不是运行
可用性，而是信号区分度：

1. 累计 counter 被当 gauge 读取，绝对水平差产生 strength-10 假异常
   （如 Redis `total_commands_processed` 4.8e9、"395546943 vs 83"）。
2. 准零序列（baseline 恒 0、MAD=0、threshold=ε）下任何单点 blip 都被
   clamp 成 strength 10，strength 失去排名区分度，attribution 平局退化为
   onset 升序，窗口首样本段系统性获胜。
3. 冷审证明：窗口首样本段在 baseline-aware 语义下 onset 可辨识（前置
   baseline 样本正常），"edge onset 不可辨识"规则对这些案例不起作用；
   真实信号与噪声 blip 在信号语义层同分同量级，进一步区分只能动 strength
   计算或 family 平局顺序，属于权重/规则调参，且分析已接触官方评分结果，
   存在案例拟合风险。

**决策**：用户于 2026-07-31 接受现状并归档。V10 成果定格为生产七场景
Gate 7/7（含 onset ≤60s、零答案泄漏）与上述基础设施能力；OpenRCA
targeted Gate 失败如实记录。T10 按停止规则未执行。

## 2. 数据来源与边界

### 2.1 冻结工件

分析对象：`D:\data\OpenRCA\results-v10-shared-signal\run-20260731T123348473710Z`

| 工件 | SHA-256 |
| --- | --- |
| `run-manifest.json` | `9e95a54fca99545600276a4a4d2f18a21515ef004649bf6fb782e30966b3b744` |
| `summary.json` | `d0b1fff74706c6e98c6262933e8866db5f420419a7b7acdf75d9874435a48080` |
| `fixed-predictions.csv` | `debedc003af1454d44de052bad692e800fb0a1b8d6bf0d789b3ee9b2a3f1f000` |
| `official-report.csv` | `366d64d4619f82d423e889cf0ac4bce31d7d49098c442dcdb62d241a185cb109` |
| `compatible-report.csv` | `8a125eaba6d22f46ada8e6d45b64d415c45602474e74f3c4f3bfcb427ad7abaa` |

Source identity：`6a7df40e962e514167bc539f20d89467a0f70abb`（branch
`agent/v10-shared-signal-semantics`）；frozen safe-index SHA-256
`b249e2f6b0b0dbd3b9f30aa71ef3302ff2c48a05cc50b914fb3dcbbc8800ad4b`；official
evaluator commit `c1bd4af7f635171a1c31cdd567c07d698dff6abc`。

T8 生产 Gate 通过工件：
`output/production-acceptance/run-20260731T111932207Z/result.json`，SHA-256
`2c3a543a3816978ee9c16d335ecf145f97eb26ffb5441e244b652a7fb258e2a9`（7/7，
总耗时 105.2s，新三场景 onset error 29.7–32.5s）。

### 2.2 分析范围

- 依据 run 内持久化 Evidence payload（Runtime SQLite）与官方报告逐案例对比。
- Ground Truth 只用于事后评分与归因，未进入诊断输入，也未用于任何代码修改。
- 六案例 Ground Truth 已在 V9/V10 两轮分析中暴露，只能作为开发集，不能再
  作为无偏效果证明。

## 3. 总体数据

| 指标 | V10 fixed | V9 fixed（对照） | 判断 |
| --- | ---: | ---: | --- |
| strict accuracy | 0/6 | 0/6 | 无满分案例 |
| partial（官方报告行均分） | 2/6 正分 | 1/6 正分（0.5） | 小幅提升 |
| component score | 0.25 | 0 | 提升 |
| reason score | 0.33 | 0 | 提升 |
| time score | 0.00 | 0 | 未改善 |
| Evidence 引用有效率 | 100% | 100% | 保持 |
| projection error/fallback | 0/0 | — | 新增契约达标 |
| 只读违规 | 0 | 0 | 保持 |
| Model tokens/cost | 0/0 | — | key-free deterministic 达标 |

## 4. 六个案例

### 4.1 Time：`Bank:16`（truth 09:18:00 ±1min，预测 09:00:00，0 分）

run 证据中 `network_latency` 段确实被检测到，onset 恰为 09:16/09:18
（真实故障时刻），但同分的 disk_io 单点 blip（DSKBps 3.0 vs baseline 0，
threshold=ε → strength 10）以更早 onset 赢得平局。**真实信号存在且正确，
排名机制把它埋掉了。**

### 4.2 Time：`Market/cloudbed-1:10`（truth 13:39:48/13:51:51，预测 13:30:00/13:30:01，0 分）

与 V9 相同的窗口起点塌缩模式；窗口首样本的 container 信号段赢得全部
attribution 名额。

### 4.3 Reason：`Bank:107`（truth network latency，预测 disk I/O saturation，0 分）

与 4.1 同根因：network_latency 证据存在（onsets 18:01–18:11），disk_io
窗口首样本 blip 同分先至。

### 4.4 Reason：`Market/cloudbed-1:17`（truth container memory load + 另一原因，0.5 分）

预测 `container memory load` + `container network packet corruption`，
命中其一。Attribution 数量与 memory 语义正确，第二原因偏离。

### 4.5 Component：`Bank:12`（truth MG01/Tomcat04，预测 IG02/MG02，0 分）

disk_io blip 所在组件赢得 attribution；真实组件的信号未进入 Top 排名。

### 4.6 Component：`Market/cloudbed-2:52`（truth checkoutservice/node-6，0.5 分）

预测 `node-6` + `adservice`，命中 `node-6`。组件归一化与去重已正确
（V9 中同组件重复输出的问题消失），但第二组件偏离。

## 5. 根因链路

```text
遥测 CSV → adapter（未做 counter→rate，累计值当 gauge）
  → Signal Core（median/MAD，threshold=ε 时任何 blip = strength 10，clamp 封顶）
  → family 均衡选择（区分度已在 clamp 中丢失）
  → Attribution（同分平局按 occurred_at 升序 → 窗口首样本段系统获胜）
  → Projector（忠实投影 attribution，无二次归因）
```

与 V9 的本质区别：V9 是"异常直接成为根因"，V10 是"真实根因信号被检测到
但排名输给噪声"。后者修复面更小，但剩余手段（strength 饱和修复、family
平局顺序）已触碰"不调权重"红线。

## 6. Amendment A1 与冷审（2026-07-31）

T9 失败后起草 Amendment A1（spec §19）：R17 counter→rate、R18 onset
可辨识性。独立冷审 8 项 findings（F1/F2 blocking）证明：

- R18 的二元"首样本=不可辨识"规则不是 baseline-aware 的，与生产
  `onset_unavailable` 消费语义（V2 Gate any-true 即失败）相撞；修正为
  baseline-aware 后，由于预备数据带完整 baseline 窗口，窗口首样本段的
  onset 反而可辨识——规则对失败案例不起作用。
- R17 客观正确但只清除假 traffic 异常，不改变 Bank 三案例的失败路径。
- 因此用户决定撤回 Amendment A1，接受现状归档。冷审 findings 全部记录在
  spec §16 review ledger。

### 已诊断未修复项（供未来迭代参考）

1. **counter 语义缺陷**（客观 bug）：OpenRCA adapter 对单调累计 series 未做
   差分率转换。修复方向已写入撤回的 R17，可直接复用。
2. **strength 饱和**：threshold=ε 下 clamp 10 消灭区分度。修复需要重新设计
   degenerate-baseline 下的强度语义，属于合同级变更。
3. **平局裁决**：同分 occurred_at 升序系统性偏向窗口起点。替代方案
   （family 优先级、corroboration）均有调参性质，且本次分析已被官方评分
   污染，未来设计应基于未暴露的 holdout。

## 7. V10 已证明与未证明

已证明：

- 共享 Signal Core 可同时服务 OpenRCA 与生产 Prometheus 两条路径；
- 生产七场景 Gate 7/7：cause/component/reason 命中、onset error ≤32.5s、
  Evidence 100%、只读 0、零答案泄漏、105.2s <900s；
- key-free deterministic Run 的 Runtime 持久化、Replay、审计链完整；
- 真实网络延迟类故障的 segment onset 检测精度可达分钟级（Bank:16 的
  09:16/09:18 段）。

未证明：

- OpenRCA targeted Gate ≥3/6（实际 2/6，time 0）；
- attribution 在同分噪声下的根因区分能力；
- 六案例之外的泛化（未运行 40-case，六案例已暴露）。

## 8. 明确不做

- 不动 strength 权重、family 顺序或平局规则来追分；
- 不把 Ground Truth、case ID 或固定答案写入诊断逻辑；
- 不在已暴露的六案例上反复重跑声称进步；
- 不运行 T10 40-case（停止规则）；
- 不撤回 T8 已通过的生产 Gate 结论。

## 9. 下一步

若未来继续 OpenRCA 提分，应启动新的 Full 级迭代：先冻结未暴露的 holdout
六案例，再基于本报告 §6 的三个已诊断缺陷设计合同级修复，全程隔离官方
评分。若不继续，当前结论足以支撑诚实陈述：生产路径诊断能力已由七场景
Gate 证明；OpenRCA 路径的信号检测有效，但根因排名在噪声同分场景下不足。
