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

### 13.Remove replayable context compaction v1 and fix workspace/checkpoint tests（26/8/16）

- 删除 v1 Replayable Context Compaction 整条实验支线：`apricity/context`、`apricity/evaluation/context_compaction`、`benchmarks/context_compaction`、旧 fixture、5 个旧测试、3 个旧脚本，以及 replayable PRD、架构文档和 ADR 0002；生产 runtime 不依赖该代码。
- 新增 `docs/experiments/context-horizon-v3.md`，保留 v1 复盘数据、失败原因与证据归档规则，并预注册 v3 的 Horizon 实验设计、A/C/E/D 条件、成功判据和停止线。
- 修复测试工作区被 `git rev-parse` 漂移到父仓库的问题：测试 helper 使用 `repo_root_override=tmp_path`，runtime `refresh_prefix` 保持既有 `repo_root_override`。
- 修复长 prefix 裁剪时 checkpoint 被 head-clip 丢弃的问题：checkpoint 文本改为独立追加在 prefix 之后，不参与 prefix 预算裁剪。
- 删除后验证：`compileall`、`ruff`、全量 pytest `192 passed, 1 skipped`，CLI `--help` 正常，90 次确定性 recovery 实验仍为 compatible 80% / resilient 100%、恢复率提升 25pp。

### 14.Add lean SessionLedger and context policies A/C（26/8/16）

- 新建精简版 `apricity/context`：append-only `SessionLedger` + Surface 折叠 + 原子替换、TokenMeter/ContextPolicy、内部 Surface planner、确定性工具结果裁剪，以及唯一外部接口 `ContextEngine.prepare`。
- 实现 Context Horizon v3 的 A `window`（超预算移除最旧完整单元）和 C `prune`（先裁剪 oversized 文本工具结果）条件；policy 不接收任何 hidden verifier 或 critical-fact 谓词。
- 新增接口层测试 `test_context_ledger.py`、`test_context_policy.py`，覆盖不可变事件、替换投影、持久化 round-trip、tool pairing、A 窗口移除、C 裁剪与 `no_safe_range`。
- 验证：`compileall`、`ruff` 通过，全量 pytest `203 passed, 1 skipped`。

### 15.Add structural compaction condition E（26/8/16）

- 在 `apricity/context` 内部 seam 增加 LLM-free 前缀压缩：把旧 Surface prefix 折叠为一个确定性、带 `[seq]` 引用的结构 capsule；保留用户要求与工具诊断行，丢弃冗余输出，源事件不删除。
- 条件 E = C + 结构压缩；压缩后不再允许整单元删除回退，仍超压时抛 `no_safe_range`，避免静默丢弃已压缩事实。
- 把前缀压缩事务与 window 单元移除分别下沉到 `structural.py` / `planner.py`，`engine.py` 保持 298 行（≤300）；`apricity/context` 总计 1,198 行，文档上限调整为 1,500 以容纳后续 D 条件。
- 新增 E 的接口测试：需求保留、诊断保留、噪声丢弃、源事件保留、tool pairing、确定性哈希。
- 验证：`compileall`、`ruff` 通过，全量 pytest `205 passed, 1 skipped`。
