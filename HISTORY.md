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

增加了一组小型真实模型测试，任务定义：[real_model_tasks.json](benchmarks/real_model_tasks.json)

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

### 16.Add Context Horizon task suite and local pressure calibration（26/8/16）

- 新增 `apricity/evaluation/context_horizon`：任务 schema v1、确定性 seed 生成、hidden verifier 加载、本地 scripted-model pilot runner、context/artifact/mechanics verifier 与校准聚合。
- 新增 `benchmarks/context_horizon/tasks.v1.json`、6 个隐藏 verifier、6 个冻结 seed JSONL、`tests/fixtures/context_horizon_repo`，以及统一入口 `scripts/run_context_horizon_experiment.py`（freeze-seeds / pilot）。
- 关键事实只存在于 hidden verifier，任务文件与 policy 代码均不携带；seed 事实按步骤分布，保证 A 在旧 prefix 上真实丢失事实。
- 本地校准结果：6/6 任务 C/E 均触发 Pressure；A 在 L2/L3/L5/L6 丢失早期事实；校准 gate 通过。
- 验证：`compileall`、`ruff` 通过，全量 pytest `210 passed, 1 skipped`。

### 17.Add chunked evidence-anchored LLM compaction condition D（26/8/16）

- 新增 `apricity/context/chunked.py`：按完成 step 分块调用 summarizer，每块最多 2,000 字符 prompt，confirmed fact 必须有 `event_seq + 精确 quote`；渲染摘要时把 evidence 原文一并呈现，保证 predicate 事实在压缩后仍可见。
- 无效摘要/坏 anchor 的 chunk 自动回退到 E 结构压缩；任何 chunk 无法安全回退则整笔不提交，仍超压时抛 `no_safe_range`。
- runner 新增 D policy 与确定性 `ScriptedChunkSummarizer`，本地 pilot 覆盖 A/C/E/D；D/E 在 6/6 任务保留全部 context facts，A 在 4/6 任务丢事实，校准 gate 通过。
- 新增 D 接口测试：锚点事实保留、噪声丢弃、非法摘要回退、tool pairing。
- 验证：`compileall`、`ruff` 通过，全量 pytest `212 passed, 1 skipped`。

### 18.Add paid Context Horizon runner with cost guard（26/8/16）

- 新增 `apricity/evaluation/context_horizon/paid.py`：真实模型 run、real process/shell 工具执行、`ModelSummarizer`、缓存感知的 ¥15 成本守卫（输入 miss 1.4 / hit 0.05 / 输出 4.5 每百万 token）。
- 脚本新增 `formal` 子命令：freeze task hash、检查工作区干净、balanced schedule、manifest 记录 commit/model/价格/cost totals。
- 新增 CostGuard 与 fake-client 付费 run 测试；全量 pytest `214 passed, 1 skipped`。
- 修复 DeepSeek 真实输出使用双竖线 `｜｜DSML｜｜` 标记时被误判为 malformed 的问题，并补充双竖线解析测试。

### 19.Run paid Context Horizon main experiment and report（26/8/17）

- 付费主实验：deepseek-v4-flash，6 任务 × A/C/E/D = 24 runs，总花费 ¥0.61（远低于 ¥15 上限），缓存命中输入 253,312 tokens。
- 首轮结果：A run-pass 0/6、fact retention 41.7%；E run-pass 3/6、fact retention 94.4%、成本 0.17 低于 A 的 0.21；C/D 同样在 fact retention 上显著优于 A。n=1/格，artifact verifier 仅检查隐藏必需事实，不声称生产可靠性。
- 产出 `analysis.json` 与 `report.md`；README 增加 Context Horizon 研究实验小节。
- 修复 D1 误删 README 正文的问题：从 `fb1cbb4^` 恢复 308 行正文后追加实验结果。

### 20.Package review pack and clean local temp products（26/8/17）

- 重写 `docs/review-pack/README.md`：30 秒 pitch、10 分钟复现路径、生产/实验架构图、证据索引、5 个高频面试追问的代码定位，以及诚实限制。
- 清理本地临时产物：`artifacts/`、`readme_intro_locked/`、全部 `tests/pytest/*`（重建空目录）、`.apricity/runs|sessions`、`__pycache__`、`.ruff_cache`；保留全部 `benchmarks/results/**`。
- 将 `ziliao/`、面试题、`Redis.md`、`tiwen.md`、`yijian.md` 移到仓库外 `D:\Pythonprojects\yan_1\jobhunt\`，并创建本地求职漏斗 `funnel.md`。

### 21.Review audit fixes and keep repository state consistent（26/8/17）

- 修复 pytest 临时目录向上扫描到仓库 `.env` 导致的测试污染：新增共享 fixture `isolated_project_env`，把项目环境发现限定在 `tmp_path`，并应用到 12 个受影响的 CLI/provider/secret 测试及 1 个潜在受影响的公共 API 测试。
- 对齐 `test_reviewer_skeleton_docs_exist` 与重写后的 review pack：改为校验 `30-second pitch`、`10-minute path`、`Architecture map`、`Evidence index`、`Interview questions`、`Honest limitations`；证据索引更新为 `219 passed, 1 skipped`。
- 付费 Context Horizon 行补齐 `pressure_crossed`/`no_safe_range`，避免共享 `summarize_rows` 在 paid-summary.json 中写出全 false 的压力字段。
- `CostGuard` 在 provider 只上报 cache hit 时从 input tokens 推导 miss tokens；`SessionLedger.load` 改为复用 `_accept` 校验，拒绝非连续 seq 或缺失 source 的持久化 ledger。
- 结构压缩 E 的 `minimum_compaction_gain` 改为与 D 一致的 token 口径；`.env.example` 不再声称内置默认 URL；README/review-pack 的 pytest 命令按 AGENTS 规则带 `--basetemp tests/pytest/<task-id>`；修正 HISTORY 中的 Windows 绝对路径 markdown 链接。
- 验证：`compileall`、`ruff` 通过，全量 pytest `219 passed, 1 skipped`。

### 22.Add streaming output for four providers（26/8/18）

- 新增 `apricity/streaming.py`：`StreamingResponseParser` 按“首个协议标记 commit”增量解析 `<final>`/`<tool>`/DSML，tool envelope 整体缓冲并限制在 1 MiB；`IncrementalRedactor` 对可见文本做跨 chunk 脱敏。
- 四个 provider adapter 新增 `stream_complete`：逐行消费 SSE/NDJSON，支持普通 JSON fallback；DeepSeek 流式请求带 `stream_options.include_usage=true`，空响应不再复制 2048 token 放大重试。
- `AgentLoop` 接入流事件：工具 candidate 等 `response_completed` 后才执行，`first_token` + `stream_summary` 聚合 trace；可见文本中断后禁止自动重试，新增 `stream_interrupted` / `stream_cancelled` stop reason 与 `partial_visible` / `visible_chars` / `request_id` 字段。
- CLI 新增互斥 `--stream` / `--no-stream`：TTY 默认流式，非 TTY 默认完整响应；`Apricity.ask(message, event_sink=...)` 作为实验性接口，旧同步 API 与 FakeModelClient 保持兼容。
- 新增 `tests/test_streaming.py` 覆盖 parser、脱敏、AgentLoop、四 provider 流式 adapter 与 CLI 参数。验证：`compileall`、`ruff` 通过，全量 pytest `238 passed, 1 skipped`。

### 23.Harden streaming semantics and Stage 2 benchmark prep（26/8/18）

- 修正 `partial_visible` 语义：正常完成保持 `false`，仅在断流/取消且已展示 partial final 时置 `true`；补充正常完成、BrokenPipe、Ctrl-C、缺失 `response_completed` 三态断言。
- 流式传输改为增量 UTF-8 行解码器，任意字节分块下多字节字符跨 chunk 不损坏；SSE 兼容 `\r\n`、多行 `data:`、重复 done 与 malformed line，JSON fallback 覆盖四个 provider。
- `BenchmarkEvaluator` / real-model benchmark runner 支持 `stream` 参数，行记录新增 `ttft_ms`、`model_response_ms`、`partial_visible`、`visible_chars`、`stream_summary`；summary 新增 TTFT、partial/cancel 与 provider error 指标。
- 新增 `benchmarks/streaming_stage2_tasks.json`（前 5 个 real-model 任务）与 `scripts/run_streaming_stage2.py`，用于 PRD Stage 2 的 streaming vs `--no-stream` 小矩阵。验证：`compileall`、`ruff` 通过，全量 pytest `254 passed, 1 skipped`。

### 24.Run DeepSeek streaming Stage 2 first wave（26/8/18）

- 使用 deepseek-v4-flash 运行 PRD Stage 2 首轮小矩阵：5 个真实模型任务 × 每条件 1 次，streaming 与 `--no-stream` 各 5 次。产物位于 `benchmarks/results/streaming-stage2-2026-08-18-205103/`。
- 结果：两组 pass rate 均为 80%；streaming 平均 TTFT 1.90s，相比 no-stream 平均单轮模型响应 27.12s 显著下降；provider 错误率、partial/cancel、重复副作用率均为 0；平均总耗时 34.29s vs 36.41s。
- 首轮 runner 在模型运行完成后渲染汇总报告时触发 `KeyError('no_stream')`，已保留原 run 全部证据；修复后通过 `--from-existing` 复用原工件完成汇总，未重复调用 provider，并在 summary 中记录 recovery 原因。
- 验证：API key 未出现在 Stage 2 任意 artifact；canonical run 前 git 工作区干净；`compileall`、`ruff` 通过，全量 pytest `254 passed, 1 skipped`。
