**本文档用来记录myapricity项目代码提交github仓库的信息**

### **1.Initial commit**（26/7/12）

### 2.Remove right.codes relay configuration（26/7/12）

### 3.unify LLM provider configuration and validation（26/7/12）

### 4.Fix provider tests and Windows command execution（26/7/12）

- 修复了提交3带来的测试问题

### 5.Fix symlink test and locale test（26/7/12）

### 6.Improve multilingual memory retrieval（26/7/14）

- 升级 Apricity 的记忆召回机制，加入 Unicode NFKC 规范化、中文 n-gram、路径与标识符特征、虚拟双语标签和分字段评分。同时增加最低召回阈值、近重复去重、Top 3 多样性控制及可审计召回 metadata，并补充中英文正负测试和双语记忆 benchmark。实现不依赖 embedding，不增加外部依赖，也无需迁移原有记忆数据。

### 7.Add DeepSeek prompt-cache telemetry(26/7/18)

- 为 Apricity 增加 DeepSeek 原生 Chat Completions 客户端，支持读取 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`。统一 OpenAI 与 DeepSeek 的缓存统计字段，包括缓存命中状态、命中 token、未命中 token 和 token 缓存命中率。
- OpenAI 继续使用显式缓存 key，DeepSeek 使用自动前缀缓存。保留原有 `APRICITY_PROVIDER`、`APRICITY_MODEL`、`APRICITY_BASE_URL` 和 `APRICITY_API_KEY` 配置格式，并兼容 DeepSeek 旧的 Anthropic endpoint。
- 同时修正缓存指标聚合逻辑，排除没有缓存遥测的请求，并补充 provider 路由、请求 payload、usage 解析及缓存统计测试。最终测试结果为 `166 passed, 1 skipped`。

### 8.rename：apricity（26/7/18）

### 9.fix：**Adding a new benchmark: evaluating small real-world models.**（26/7/21）

增加了一组小型真实模型测试，任务定义：[real_model_tasks.json](D:\Pythonprojects\yan_1\agent\mypico\benchmarks\real_model_tasks.json)

### 10.Add resilient harness recovery experiments（26/8/14）

- 为 Apricity 引入类型化的生命周期恢复机制（`RecoveryEngine`），把恢复决策从 AgentLoop 和 provider 适配器中解耦，统一处理模型、协议与工具结果三类失败，支持 provider 重试、上下文恢复、工具结果溢出（spill）与协议纠正四类策略。
- 提供兼容（compatible）与弹性（resilient）两种模式及全局恢复预算，内置确定性故障注入（瞬时 provider 错误、上下文溢出、超大工具结果、畸形响应），并运行 compatibility/resilient 与单策略消融（ablation）对比实验。
- 补充恢复策略 PRD、架构文档与 ADR（typed recovery seam），新增恢复实验与基准运行脚本及恢复测试，产出实验数据 `recovery-policy-experiment-v1.json`。实验结果：resilient 模式通过率 100%、恢复率 100%，相对 compatible 模式通过率提升 20%、恢复率提升 25%，无虚假干预与重复副作用。

### 11.Harden compaction resume provenance（26/8/16）

- 为 Replayable Context Compaction runner 补齐 provider、model、代码 commit、fixture/pressure hash 与完成 run artifact hash 的 provenance 校验，防止不一致配置或被修改的证据被静默 resume。
- 为每个失败 run 持久化唯一 `primary_failure`，区分 context token estimator 与 provider usage meter，并恢复 `tests/pytest` 的 pytest 收集隔离，避免临时 fixture 被递归收集。

### 12.Render compaction report evidence and D-A bootstrap（26/8/16）

- 报告层补齐阶段三要求：report.md 现在渲染 artifact hashes、provider/model/version、public verifier 的 pytest cache telemetry、mean/median/P95 延迟、main/summary/combined token totals、overflow 原始计数，以及 D-A delta 的 task-clustered bootstrap 95% CI。
- runner 在 public verifier 子进程前后采集 `.pytest_cache` 目录，manifest 新增 `version` 字段并纳入 config hash；metrics 增加延迟分位数、token 汇总、cache telemetry 聚合与按 task 聚类的 bootstrap。

