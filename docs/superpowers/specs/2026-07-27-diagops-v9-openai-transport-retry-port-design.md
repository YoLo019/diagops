# DiagOps V9 OpenAI 传输重试移植设计

## 1. 背景

V9 Batch04 的 Fixed `Market/cloudbed-1:37` 在原始运行、三 case recovery 和单 case
recovery 中连续出现 `APIConnectionError`。最近两次失败分别位于
`conflict_review` 和 `CoordinatorAgent` round-2 `final_synthesis`；前序模型调用、
Runtime checkpoint 和 Model lifecycle 均正常闭合。这排除了固定 case 或固定 phase
缺陷，指向瞬时传输失败。

V9 当前 `openai_responses_model` 显式配置 `AsyncOpenAI(max_retries=0)`。V8.2
 sibling 分支已经通过提交 `38b118270542c3449c951b8f8425d618ee44fa59`
验证了两次 SDK 级传输重试，但该提交不是 V9 的祖先，因此 V9 未获得此修复。

## 2. 方案比较

1. 将 V8.2 已验证的 `max_retries=2` 最小行为移植到 V9。SDK 只重试失败请求，
   继续受 180 秒 Runtime 总期限约束；采用。
2. 在 Agents Runtime 外层重跑整个 turn。需要处理重复 token、事件和持久化状态，
   风险和改动均更大；不采用。
3. 保持 `max_retries=0` 并反复人工补跑。三次同类失败已证明成本不可控；不采用。

## 3. 决策与边界

仅修改 `backend/diagnosis/openai_model.py`：

- 将 `AsyncOpenAI(max_retries=0)` 改为 `max_retries=2`；
- 同步更新函数文档注释；
- 更新现有构造参数单测的名称和期望值。

不增加配置项、自定义重试器、额外日志、整体 turn 重跑或输出降级。两次重试耗尽后，
现有 `FailureCategory.TRANSPORT` 和严格空预测拒绝语义保持不变。认证、配额、限流、
Evidence 合同、180 秒总期限及 transport timeout 计算均不改变。

## 4. 验证与产物

按 TDD 先让现有单测期望 `max_retries=2` 并确认旧代码失败，再应用生产代码改动。
随后运行目标测试、完整 pytest 和 Ruff。修复单独提交，形成新的 V9 commit。

只为 Fixed `Market/cloudbed-1:37` 创建新的不可覆盖 recovery 输出、Runtime 数据库和
transcript 路径；现有 Batch04、三 case recovery 和失败的单 case recovery 全部保留。
新 recovery 通过零连接错误、非空预测、完整 Runtime/Attempt、Model lifecycle、
Evidence 引用和 token 对账门槛后，才继续 Adaptive recovery。
