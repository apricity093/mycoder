# Apricity 流式输出 PRD

## 1. 文档信息

- **版本**：v1.1
- **日期**：2026-08-18
- **状态**：已完成技术审查，待开发排期
- **产品范围**：`apricity` CLI、`Apricity` runtime、provider adapters
- **目标发布形态**：默认启用流式传输和终端增量输出；保留现有同步 API 的兼容行为

## 2. 背景与问题

当前一次模型调用通过统一的 `model_client.complete()` 返回完整字符串。Ollama、OpenAI-compatible、DeepSeek 和 Anthropic-compatible adapter 都等待 HTTP body 完整到达后才返回；`AgentLoop` 随后才解析 `<tool>...</tool>` 或 `<final>...</final>`，CLI 最后一次性 `print(agent.ask(...))`。

这会造成三个问题：

1. 用户只能在模型生成全部内容后看到结果，首 token 延迟（TTFT）不可感知。
2. 长答案没有进度反馈，用户容易误以为进程卡住。
3. provider 已经可能返回 SSE/NDJSON，却在 adapter 层被重新聚合，失去传输层的收益。

本需求不是简单把请求体中的 `stream` 改成 `true`。Apricity 的模型响应同时承担“最终回答”和“工具控制协议”两种职责；工具调用必须在完整、合法、通过现有安全检查后才能执行，不能把 `<tool>` 标记或半截参数直接展示给用户。

## 3. 产品目标

- 对四类 provider 使用原生流式协议，边接收边处理模型事件。
- 对最终回答提供增量终端输出，明显降低用户感知的 TTFT。
- 保持现有工具调用、审批、恢复、checkpoint、session history、trace/report 和安全边界不变。
- 保持 `Apricity.ask(message) -> str` 的同步兼容接口；旧的 FakeModelClient 和第三方 adapter 不必立即改造。
- 流式失败、取消、协议错误都能落盘并可诊断，不能静默吞掉原始错误或把半截答案记成已完成答案。

## 4. 非目标

- 不改变模型提示词、`<tool>` / `<final>` 协议语义和工具权限。
- 不在本期实现 WebSocket、HTTP 服务端推送、浏览器 UI 或多用户会话。
- 不展示模型 reasoning、隐藏协议字段、工具参数、API key 或 provider 原始响应。
- 不为已经产生不确定副作用的工具调用自动重试。
- 不要求所有 provider 具有完全相同的底层事件字段；差异由 adapter 屏蔽。

## 5. 用户与使用场景

### 5.1 终端用户

运行 `apricity` 交互模式或 one-shot 命令，期望最终回答逐字出现；工具执行期间看到明确但简短的状态提示，不看到内部 XML/JSON。

### 5.2 Python API 用户

继续调用 `agent.ask("...")` 并获得完整字符串；需要 UI 或日志集成时，可订阅可选的流事件，不被迫改成异步 API。

### 5.3 维护者

可以从 trace 区分请求开始、首个文本增量、工具调用解析、完成、取消和失败，并比较 provider 的 TTFT、总耗时和实际输出 token。

## 6. 用户体验与交互要求

1. 每次模型请求开始时，CLI 不打印 provider 原始事件，只保留现有必要的提示。
2. 识别到 `<final>` 后，终端按文本增量输出；增量之间不得人为添加空格或换行。
3. `<tool>...</tool>`、DSML invoke 和 XML 属性格式全部缓冲，不向终端输出。
4. 工具调用完成后，保留现有工具执行提示策略；下一次模型请求仍可流式输出。
5. 最终答案完成时输出一次换行，确保下一个 `apricity>` 提示符不与答案粘连。
6. `/help`、`/memory`、`/session`、`/reset`、`/exit` 行为不变。
7. Ctrl-C：若尚未产生可见 final 文本，立即取消请求并恢复 REPL；若已有可见文本，保留已显示内容，追加一次“输出已取消”提示，并将本轮标记为失败/取消，不写入 completed assistant history。
8. 交互式 TTY 默认启用流式；非 TTY（管道、重定向、CI）默认走非流式纯文本，必须显式传 `--stream` 才启用流式。任何场景都不输出 ANSI spinner。

## 7. 功能需求

### 7.1 统一流事件合同

provider adapter 新增 `stream_complete(prompt, max_new_tokens, **kwargs)`，返回按顺序产生的事件。建议事件类型：

| 事件 | 必填字段 | 说明 |
| --- | --- | --- |
| `response_started` | `request_id`, `provider`, `model` | HTTP 响应建立 |
| `text_delta` | `text`, `channel` | `channel` 为 `protocol` 或 `final`；只有 `final` 可展示 |
| `tool_candidate_ready` | `raw_text` 或结构化候选 | 已收到完整工具 envelope，交给现有 `parse()` |
| `response_completed` | `metadata`, `finish_reason` | 传输和协议均正常结束 |
| `response_failed` | `error_type`, `message`, `retryable`, `request_id` | 失败必须保留原始错误证据；是否已展示由 runtime 判定 |

事件对象应为轻量、可序列化的数据结构。未知 provider 事件必须被忽略或归一化，不能让 provider 字段泄漏到 AgentLoop。

### 7.2 用户可见输出接口

- `Apricity.ask(message)` 保持返回完整最终字符串。
- `Apricity.ask(message, event_sink=callable)` 增加可选事件回调；回调异常不得破坏 agent 主循环，需记录为 `sink_error`。
- CLI 默认传入终端 sink；`--no-stream` 不传 sink，走现有完整响应路径。
- 不要求本期引入 async；provider 使用同步迭代器即可。
- `BrokenPipeError` 或 stdout 不可写不按普通 `sink_error` 继续生成；CLI sink 必须停止消费并取消当前 provider generator，最终记录 `cancelled=true`。

### 7.3 增量协议解析

新增独立的 `StreamingResponseParser`，以 chunk 为输入、以“可展示文本/完整工具候选/终态”事件为输出：

1. 使用增量 UTF-8 decoder，正确处理多字节字符跨网络 chunk；SSE 兼容 `\r\n`、多行 `data:` 拼接、`event:` 忽略、空 chunk 和重复 done。
2. 采用“首个协议标记 commit”规则：最先确认的 DSML invoke、`<tool>` 或 `<final>` 决定本轮协议类型；commit 后忽略后续其他协议标记。这一规则同时写入 `Apricity.parse()`，保证流式和非流式语义一致。必须覆盖“`<final>` 开头 + DSML 结尾”“`<final>` 开头 + `<tool>` 结尾”“DSML 开头 + `<final>` 结尾”三组 golden test。
3. 确认 `<final>` 开始后，仅发出 final 内容；末尾 `</final>` 的匹配窗口不得展示。普通文本 fallback 不立即展示：只有在 response 完成且全量文本仍未出现协议标记时，才一次性按 final 返回，避免后续 tool/DSML 反转语义。
4. 完整 `<tool>`、DSML invoke、XML 风格 tool 都必须整体缓冲；收到闭合标记后产出 candidate，但不得执行工具，直到收到 `response_completed` 并关闭/耗尽底层 generator。
5. tool envelope 默认最大缓冲为 1 MiB（按 UTF-8 bytes 计）；超过上限进入协议错误，保留脱敏后的截断 raw 证据，不得无限增长内存。该上限首版只作为内部常量，不新增 CLI 配置。
6. response 完成时若 final 为空、标记未闭合或协议不合法，返回现有 `retry` 语义，不得把半截 XML 当作最终答案。
7. parser 必须能输出 `raw_text` 供 trace/诊断；可见文本也必须经过带 carry buffer 的增量 redactor，至少保留 `max_secret_len - 1` 个尾字符后再 flush，防止 secret 跨 chunk 泄漏。

### 7.4 AgentLoop 生命周期

- 在 `model_requested` 后消费 `stream_complete()`，于首个可见 `final` delta 时发出 `first_token` trace。
- 收到完整 tool candidate 后暂停对外输出，进入 `TOOL_READY`；必须等 `response_completed`、usage metadata 收齐且 HTTP generator 已关闭/耗尽后，再复用当前 `parse`、工具校验、审批和执行流程。
- 收到完整 final 后才执行现有 `record assistant`、memory promotion、checkpoint 和 `run_finished`。
- `response_failed` 继续交给现有 `RecoveryEngine`。在没有可见 final 文本前，可按原策略重试；已经展示 final 文本后禁止自动重试，避免重复答案，终态为 `stream_interrupted`。
- 工具 candidate 已完整但尚未执行时发生连接失败，可按原 provider retry 规则重试；工具已执行或副作用不确定时不得由流式功能新增重试。重试只允许发生在没有可见 final 文本时。

### 7.5 Provider 适配

| Provider | 请求协议 | 增量字段 | 结束/用量 |
| --- | --- | --- | --- |
| Ollama | `/api/generate`, `stream=true` | NDJSON `response` | `done=true`，读取最终 usage 字段 |
| OpenAI-compatible | `/responses`, `stream=true` | `response.output_text.delta` | `response.completed` / response usage |
| DeepSeek | `/chat/completions`, `stream=true` | SSE `choices[0].delta.content` | `[DONE]` 或 usage chunk；请求显式带 `stream_options={"include_usage": true}`，拿不到 usage 时 `cache_telemetry_available=false` |
| Anthropic-compatible | `/messages`, `stream=true` | `content_block_delta.delta.text` | `message_delta` / `message_stop` |

所有 adapter 都必须：设置正确 `Accept`；逐行消费而不是 `read()` 全量 body；兼容服务端错误 JSON；保留现有 timeout、HTTP 错误分类和 `last_completion_metadata`；显式转发 `prompt_cache_key` / `prompt_cache_retention`。当服务端忽略 stream 返回普通 JSON 时，adapter 应把完整文本包装成一个 `text_delta` 后正常完成。DeepSeek 流式路径不复制现有 adapter 内“空 content 将 max_tokens 放大到 2048 后重发”的语义；空响应统一交给 AgentLoop 的 protocol correction，且仅在没有可见文本时允许重试。

### 7.6 CLI 参数与兼容性

- 新增互斥参数 `--stream` / `--no-stream`：TTY 默认等价于 `--stream`，非 TTY 默认等价于 `--no-stream`；显式参数优先于环境和自动检测。`--no-stream` 优先级最高，不创建 stream 事件、不调用 `event_sink`，直接走旧 `complete()`。
- 可选新增 `--stream-timeout` 不在本期强制；沿用各 provider 现有 timeout，避免引入两套超时语义。
- 交互 TTY 默认行为变更为流式；现有命令、环境变量、session 文件格式和同步返回值不变。
- `event_sink` 作为实验性 API，不在本期加入 `apricity.__init__.__all__`；FakeModelClient 若没有 `stream_complete`，runtime 自动把 `complete()` 结果包装成单个 delta，现有测试无需全部重写。

## 8. 状态机与异常语义

```text
REQUESTED
  -> CONNECTED
  -> BUFFERING_PROTOCOL
  -> FINAL_STREAMING -> COMPLETED
  -> TOOL_READY -> WAIT_COMPLETED -> TOOL_EXECUTED -> REQUESTED
  -> FALLBACK_BUFFERING -> COMPLETED
  -> EMPTY_RESPONSE -> PROTOCOL_RETRY
  -> FAILED_BEFORE_VISIBLE (可按策略重试)
  -> FAILED_AFTER_VISIBLE -> STREAM_INTERRUPTED (不可自动重试)
  -> CANCELLED
```

- `COMPLETED` 才能写入完整 assistant history 和 `status=completed`。
- `FAILED_BEFORE_VISIBLE` 沿用 `model_error`、`provider_retry_exhausted` 等既有 stop reason。
- `STREAM_INTERRUPTED` 和 `CANCELLED` 必须成为 `task_state.py` 的稳定 stop reason 常量；`partial_visible`、`visible_chars`、`request_id` 作为可选字段写入 task state，并兼容旧 task_state 缺省字段。已展示 partial final 后取消时，session history 写入一条 runtime notice（例如“上一轮输出在流式阶段中断”），partial 文本只进 trace/report，不伪装成已完成 assistant answer。
- 客户端断开/用户 Ctrl-C 视为取消，不把取消误记为 provider error。
- 空 chunk、重复 done、未知事件、无效 JSON 行：记录 warning 并继续；无法恢复的协议错误进入现有 protocol correction。

建议新增稳定常量 `STOP_REASON_STREAM_INTERRUPTED` 与 `STOP_REASON_STREAM_CANCELLED`；旧 task state 缺省这些字段时按空值读取，不能因 schema 扩展导致 resume 失败。

## 9. 持久化、日志与可观测性

trace 新增或补充以下事件字段：`request_id`、`streaming`、`ttft_ms`、`visible_chars`、`chunk_count`、`first_delta_at`、`last_delta_at`、`finish_reason`、`partial_visible`、`cancelled`、`provider_event_count`。`request_id` 由 AgentLoop 每轮生成并传给 adapter；adapter 可选记录 provider 返回的 `x-request-id`。

要求：

- trace 不记录完整 API key、Authorization header 或未脱敏原始 body。
- `model_parsed` 只在完整 tool/final 可解析时发出；不按 delta 落盘。响应结束时只写一次 `stream_summary`，聚合 `chunk_count`、`visible_chars`、首尾时间和 provider event 数；如需诊断原始序列，最多按每秒或每 N 个 chunk 抽样。所有可见增量先经过跨 chunk redactor，不能直接调用仅支持完整字符串的 `redact_text()`。
- report 同时记录 `streaming=true/false`，以支持 A/B 对比。
- 失败 run、原始错误和关联 request_id 必须保留；重跑不得删除或覆盖旧 run。

## 10. 性能目标与安全护栏

首版验收目标：确定性 HTTP fixture / FakeModelClient 每组至少 30 次；真实 provider 每个 provider 5–10 次小型矩阵：

- TTFT 基线固定为：流式组 `model_started_at → 第一个可见 final delta 送达 sink`；非流式组 `model_started_at → complete() 返回`。流式组 P50 相比非流式降低至少 30%，且不得增加请求总 token 消耗。
- 正常完成总耗时回归不超过 10%。
- parser/sink 使用 `StringIO` 或 null sink 测量，不能用真实终端刷新速度判断；可见输出消费吞吐不低于 provider 实际 delta 到达速度的 95%。
- 工具执行、审批拒绝、重复调用防护、路径安全、脱敏和 checkpoint 测试全部保持通过。
- 流式功能不得改变工具调用次数、工具参数或副作用；重复副作用率必须为 0。

## 11. 测试计划

### 单元测试

- parser：标签跨 chunk、Unicode 跨 chunk、final 尾标记、tool/DSML/XML 三种协议、空 final、普通文本 fallback。
- provider：SSE、NDJSON、普通 JSON fallback、HTTP 错误、断流、malformed line、usage/cache telemetry。
- sink：回调异常隔离、不可写 stdout、TTY/非 TTY、`--no-stream`。

### 集成测试

- FakeModelClient 产生多个 delta，`agent.ask()` 返回与非流式完全相同的最终字符串。
- 工具调用全过程不向 sink 泄漏协议内容，工具执行后下一轮仍可流式 final。
- 可见 final 之前断流会触发既有 retry；可见 final 之后断流不会重复请求。
- Ctrl-C、空响应、provider fallback、session resume、report/trace 字段。

### 回归与基准

使用 `uv run pytest tests -q --basetemp tests/pytest/<唯一运行名>`；所有正式 benchmark 结果只写入 `benchmarks/results/<新的唯一目录>/`。canonical benchmark 前必须确认 `git status --short` 为空。对照组至少包含 `--no-stream` 与默认流式两组，并记录 provider、model、重复次数、TTFT、总耗时、token、完成率和取消/失败原因。

## 12. 验收标准（Definition of Done）

- 四个 provider 均可在真实或确定性 fixture 中逐增量消费；普通 JSON fallback 仍可用。
- 默认 CLI 能看到 final 增量文本，且绝不看到 `<tool>`、DSML、Authorization 或 reasoning 内容。
- `Apricity.ask()`、现有 session/history/report schema 和旧 FakeModelClient 测试保持兼容。
- 断流、取消、协议错误的 stop reason、trace、report 和原始错误证据齐全。
- 所有现有测试通过；新增 parser/provider 模块行覆盖率 ≥ 90%，断流、空响应、malformed line、普通 JSON fallback、取消、尾标记缺失等每个错误分支都有命名测试。
- 文档、README 使用说明、HISTORY 记录和发布说明同步更新。

## 13. 发布与回滚

1. 第一阶段：实现事件合同、首个协议标记 parser、聚合 trace 和 Fake/HTTP fixture；默认 `--no-stream`。退出条件：现有测试全绿，新增 parser/provider 行覆盖率 ≥ 90%，三组混合协议 golden test 全绿。
2. 第二阶段：TTY 默认流式，非 TTY 默认非流式；四个 provider 灰度运行，收集 TTFT/失败率/重复副作用指标。退出条件：无协议泄漏、重复副作用率为 0、流式失败率不高于对照组 3 个百分点。
3. 第三阶段：扩大真实 provider 使用范围，保留 `--no-stream` 作为稳定回滚开关。退出条件：性能目标达成且无未关闭 P0/P1。

出现 provider 兼容性、输出乱码、工具泄漏或失败率回归时，优先使用 `--no-stream` 回退；不得通过删除失败 run 或静默 resume 隐藏问题。修复后用新的唯一 benchmark 目录重跑，并在报告中关联原 run。

## 14. 已决策项

以上 4 项已在本版本拍板：采用实验性 `event_sink`；沿用现有 timeout，本期不新增独立 read timeout；非 TTY 默认关闭流式、由 `--stream` 显式开启；parser 事件暂不导出到 `apricity.__init__`。后续若要改变这些默认值，需单独更新版本化 PRD。
