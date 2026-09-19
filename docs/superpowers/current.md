# DiagOps 当前版本

更新：2026-09-17。

## V12

目标：修复 V11 的设计、实现和测试，使用 OB30 的 30 个开发案例做轻量效果对照。

- [Spec](specs/2026-09-12-diagops-v12-validation-design.md)：已同步实现与验证范围。
- [Plan](plans/2026-09-12-diagops-v12-validation-plan.md)：当前按用户要求先放宽预算验证 OB30 全流程，再依据实际消耗调整；Single／Multi 均为 200,000 Tokens、48 次模型请求、24 次工具调用、300 秒，额外 Lead 为 60,000 Tokens、12 次请求、180 秒。三路两轮边界不变。
- [领域术语](../../CONTEXT.md)。

默认流程为 Lead 规划、Investigator 独立取证、Critic 最终选择与排序；发布节点不额外调用 Lead。报告、API、前端、重放与评测按 final_decision 的顺序展示。额外 Lead 只在已保存的 Critic 快照上做裁决阶段对照。

已落实连续取证、限长结果的细节获取、摘要省略提示与收尾预算。产品默认仍为 16 次模型请求、120 秒、8 次逻辑工具调用；上述宽松额度用于本轮开发评测。新契约最多三次结构化纠错，连同传输重试仍最多四次尝试；恢复沿用已保存额度，历史一次纠错不被放宽。未知 usage 保守扣账，恢复不清空已用预算。

最近完成的宽松预算批次 `v12-ob30-deepseek-20260917-135242-561`：Single 30 完整；Multi 26 完整、2 inconclusive、1 partial、1 输出校验失败；额外 Lead 29 份裁决。1,430 条审计引用均有效，59 个完成运行均保留报告，29 个 Multi 持久化结果校验通过；引用的语义支持情况尚未评审。当前修复失败缓存、Critic 预留与第二轮输出，按用户要求先定向重跑上述四例 Multi 和各自的额外 Lead；缺少快照属于失败的同一例。四例核验后再启动独立完整 OB30，不合并定向结果充当全量评测。下文为早期批次记录。

定位仍是证据可追溯、执行可恢复的只读 SRE 诊断。HolmesGPT 等参考项目的能力不等于本项目全部具备；离线替身只验证运行路径，不证明真实诊断效果。

工程验证：全量 pytest 2459 passed、4 skipped；末次工具重放相关回归 63 passed、前端 smoke 8 passed、本地 canary 5 passed；Runtime acceptance 14/14，Ruff、前端构建和 diff 检查通过。日志位于 `output/v12-validation/20260913-final/`，跳过原因与验证范围见 plan。未提交 Git。

最初指定 `gpt-5.6-terra`，暂不设金额上限；真实评测保留 spec 的每 case 预算。已提供兼容 API base URL `https://www.piteai.com/v1` 和数据根目录 `D:\data\RCAEval`，用户已确认更新本地 `DIAGOPS_AGENTS_API_KEY`；读取用户密钥并启动检查的命令被自动审批拦截，随后由用户在本机启动预检。开发入口为 `backend.benchmarks.rcaeval.dev`，预测不读取标签，证据核对后单独评分。效果尚未实测，不宣称准确率提升。

已完整校验 `D:\data\RCAEval\v11-m5-ss15\runtime`，其中 OB30 共 30 个案例；标签尚未读取。首次批次 `v12-ob30-20260914-093557-119` 因九种角色格式整批预检超过 30 秒而未开始预测；脚本仅将预检时限改为 90 秒。第二批次 `D:\data\RCAEval\results\v12-ob30-20260914-094043-725` 能力检查通过，采用 strict_output_tool；真实预测反复因单次预留低估而拒绝，已停止并保留原始产物。

停止时保存了 5 份失败预测、另有 1 个未结算请求；事件中的已知诊断用量为 40,890 Tokens，原预测仅统计 4,088 Tokens。预检消耗未包含在此数，未返回请求的实际用量及金额未知。该批次只用于排查缺陷，不作为准确率结果。已修复结构化收尾 schema 漏算、估算不能上调、单次预留不足误拒绝、失败 usage 漏统计和预算失败分类；总预算与并发隔离保持原要求。

本次补充验证：相关模块 290 passed，末次分类检查 4 passed，本地 SDK canary 5 passed，Runtime acceptance 14/14；Ruff 通过。日志及原批次用量核对位于 `output/v12-validation/20260914-budget-fix/`。未重复此前的全量 pytest／前端构建，本次没有前端改动。需重新执行原本地脚本生成独立批次，不能复用修复前绑定源码的能力检查产物。

第三批次 `D:\data\RCAEval\results\v12-ob30-20260914-143701-202` 未开始预测：原生 JSON 通道和九角色整批预检超时。发现预检把分批角色请求共用一次时限，且未选通道的超时也会否决备用通道。已调整为单次请求独立计时、复合探测保留有界总时长，并只由必需项与选定通道判定截止时间；角色失败记录 schema 名称及异常类型。同时修正原生 JSON 探测仍要求旧版 planning／investigator 字段的问题，提示样例与当前 Single schema 一致。原脚本仍为单请求 90 秒，真实诊断仍为 120 秒。失败产物保留，需另存新批次验证端点实际表现。

预检修复验证：能力检查与开发评测模块 28 passed，末次原生提示／schema 一致性检查 1 passed（与前组重叠），Ruff 和 diff 检查通过；产物位于 `output/v12-validation/20260914-capability-deadline/`。本次未再次调用真实端点、未重跑全量或前端测试。

第四批次 `D:\data\RCAEval\results\v12-ob30-20260914-145420-921` 仍未启动预测。原生 JSON schema 已通过并被选用，备用 strict_output_tool 超时未计入阻断项；三路并发检查及 LeadAdjudicationOutput、FinalDecisionDraft、V11SingleControlOutput 请求超时，能力检查按预期拒绝。另一次无凭据连接检查 DNS 约 415ms、HTTPS 在约 1,820ms 返回 401，只证明基础连接可达，不证明带凭据模型请求稳定。当前证据不足以区分网关排队、账号并发限制或上游模型延迟；需核查服务端请求日志／并发额度后继续。未修改预算或放宽能力要求，未再次发出真实模型请求。

当前改用 DeepSeek 官方 API，用户明确不测思考模式。按推荐使用 `deepseek-flash`，地址为 `https://api.deepseek.com/beta`；预检、真实 SDK 调用、克隆模型及追加 Lead 均固定关闭 thinking，诊断输出 schema 不变。原本地启动脚本已切换，读取用户环境变量 `DEEPSEEK_API_KEY` 后仅传入评测进程的 `DIAGOPS_AGENTS_API_KEY`，不复用旧网关密钥。结果以 `v12-ob30-deepseek-` 新批次保存，配置记录包含请求扩展参数；Single、Multi-Critic 和 Lead 对照使用同一模型。切换时保留原预算，后续调整见下文。

DeepSeek 切换验证：兼容适配器、DeepSeek 预设、能力检查和开发评测入口共 74 passed，Ruff、启动脚本语法及 diff 检查通过。日志位于 `output/v12-validation/20260914-deepseek/`。该轮离线验证未调用真实 DeepSeek；随后用户已配置官方密钥并启动，结果见下文。未重复全量／前端检查，本次无前端修改。

DeepSeek 批次 `D:\data\RCAEval\results\v12-ob30-deepseek-20260914-155114-881` 能力检查通过，采用 strict_output_tool；原生 JSON schema 不支持是可选通道结果，不阻断。预测反复失败后已停止，保留 6 份失败预测、0 份成功预测；已知诊断消耗 44,174 Tokens，另有 2 次未知 usage 的失败请求与 1 次停止时未结算请求，不含预检消耗。旧记录缺少的实际 usage 无法补回。12,000 Tokens 曾使 Multi 在调查开始前耗尽额度，因此用户确认下一批两组共同提高至 60,000；旧批次不并入新配置的效果统计。

已修复 strict 工具响应解包后，续接历史未重新封装的问题，并使预算估算采用实际发送的封装形态；畸形 envelope 改为结构化输出失败，保留已返回 usage。Lead 单次纠错提示与允许的 inconclusive／可空字段保持一致，并记录有界字段错误码，不保存原始响应。旧 Lead 失败的具体字段不可恢复，是否解决真实失败仍待新批次验证。可选通道不支持现在显示 UNAVAILABLE，能力判定要求不变。相关回归 226 passed，Ruff 与 diff 检查通过；日志及停止批次用量核对位于 `output/v12-validation/20260914-deepseek-runtime/`。未重跑全量／前端测试，未再次调用真实模型。用户用原本地脚本重新启动，生成与当前源码绑定的新预检及预测结果。

## V11 参考

- [V11 设计](specs/2026-08-02-diagops-v11-adaptive-multi-agent-rca-design.md)
- [V11 计划](plans/2026-08-02-diagops-v11-adaptive-multi-agent-rca-implementation-plan.md)
- [SS15 修订](specs/2026-08-19-rcaeval-ss15-protocol-design.md)
- [SS15 计划](plans/2026-08-19-rcaeval-ss15-protocol-implementation-plan.md)

V11 正式 SS15／TT90 评测尚未完成，原进度和运行要求见上述文档；本版 OB30 开发评测不替代正式结果。工作区已有修复在 V12 实施时复查并复用。
