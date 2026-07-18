# pico

`pico` 是一个面向代码仓库的轻量本地 coding agent。它直接跑在终端里，先看当前工作区，再用一组受约束的工具去读文件、改文件、跑命令，并把会话状态保存在本地 `.pico/` 目录里。

它更像一个能在仓库里持续工作的命令行助手，不是纯聊天窗口。你可以拿它做代码排查、测试修复、仓库分析，或者让它在当前项目里执行一次性的工程任务。

## 适合做什么

- 在本地仓库里排查测试失败
- 读取当前代码结构并给出修改建议
- 基于现有文件做小步迭代，而不是脱离仓库空想
- 在会话中保留上下文，支持继续上一次工作

## 主要特性

- 包名是 `pico`
- CLI 命令是 `pico`
- 模块入口是 `python -m pico`
- 会话保存在 `.pico/sessions/`
- 每次运行的工件保存在 `.pico/runs/<run_id>/`
- 支持四类模型后端：
  - Ollama
  - OpenAI 兼容 Responses API
  - Anthropic 兼容 Messages API
  - DeepSeek Anthropic 兼容 API

## 使用截图

CLI 帮助信息：

![pico help](assets/screenshots/pico-help.png)

启动界面：

![pico start](assets/screenshots/pico-start.png)

REPL 内置命令与会话路径：

![pico repl](assets/screenshots/pico-repl.png)

## 安装

需要 Python 3.10+。

如果你用 `uv`，直接安装依赖：

```bash
uv sync
```

如果你已经在自己的 Python 环境里工作，也可以直接装成可编辑模式：

```bash
pip install -e .
```

## 快速开始

在当前仓库里启动交互模式。默认 provider 是 DeepSeek：

```bash
uv run pico
```

指定另一个工作目录：

```bash
uv run pico --cwd /path/to/repo
```

直接跑一次性任务：

```bash
uv run pico "inspect the test failures and propose a fix"
```

如果当前环境已经安装过包，也可以直接这样启动：

```bash
python -m pico
```

## 模型后端

Pico 启动时会读取项目根目录的 `.env`。本地真实 key 放在 `.env`，仓库只保留 `.env.example`。配置优先级是：

```text
显式 CLI 参数 > .env 里的统一 PICO_* 变量
```

Provider 选择的具体顺序是：

```text
--provider > PICO_PROVIDER > 代码默认 deepseek
```

不传 `--provider` 且没有 `PICO_PROVIDER` 时默认使用 `deepseek`。推荐配置 DeepSeek 原生 endpoint，以获得自动前缀缓存及命中 token 遥测。其他 provider 仍然保留，可以在 `.env` 里写 `PICO_PROVIDER=openai`、`PICO_PROVIDER=anthropic`、`PICO_PROVIDER=ollama`，也可以显式传 `--provider openai`、`--provider anthropic` 或 `--provider ollama`。

`.env` 会在构建 provider client 前加载，并覆盖当前进程里的同名环境变量。`model` 和 `base URL` 必须通过 `PICO_MODEL`、`PICO_BASE_URL` 或对应 CLI 参数提供；远程 provider 还必须配置 `PICO_API_KEY`。API key 只从环境变量读取。

本地第一次配置：

```bash
cp .env.example .env
```

然后配置一组 active provider 参数。`.env` 已经被 `.gitignore` 忽略，不要提交真实 key。

### 推荐配置：DeepSeek

完整配置示例：

```bash
PICO_PROVIDER="deepseek"
PICO_MODEL="deepseek-v4-pro"
PICO_BASE_URL="https://api.deepseek.com"
PICO_API_KEY="your-api-key"
```

配置完成后直接启动：

```bash
uv run pico
```

如果你需要临时切模型或代理地址，不必改 `.env`，可以直接覆盖：

```bash
uv run pico --model deepseek-v4-pro --base-url https://api.deepseek.com
```

DeepSeek 推荐走原生 Chat Completions API。该接口默认启用前缀缓存，Pico 会从 `usage.prompt_cache_hit_tokens` 和 `usage.prompt_cache_miss_tokens` 记录缓存命中情况。旧的 `https://api.deepseek.com/anthropic` 地址仍可使用，但只作为 Anthropic-compatible 兼容路径，缓存遥测会标记为不可用。

OpenAI 和 DeepSeek 仍使用完全相同的 `PICO_*` 环境变量名。分别运行实验并保存产物后，可以比较 `cache_hit_rate`、`cached_token_ratio` 和 `avg_cached_tokens`；由于两家分词器不同，优先比较前两项比例指标。

所有 provider 使用相同的环境变量名，只修改值：

| provider | base URL | API key | model |
| --- | --- | --- | --- |
| `deepseek` | `PICO_BASE_URL`，必填 | `PICO_API_KEY`，必填 | `PICO_MODEL`，必填 |
| `openai` | `PICO_BASE_URL`，必填 | `PICO_API_KEY`，必填 | `PICO_MODEL`，必填 |
| `anthropic` | `PICO_BASE_URL`，必填 | `PICO_API_KEY`，必填 | `PICO_MODEL`，必填 |
| `ollama` | `PICO_BASE_URL`，必填 | 不需要 | `PICO_MODEL`，必填 |

旧的 provider-specific 和 vendor-specific 变量不再兼容；检测到非空旧变量时，Pico 会在启动阶段给出迁移错误。

如果有额外的敏感环境变量需要从 trace/report 里脱敏，可以用 `PICO_SECRET_ENV_NAMES` 配置逗号分隔的变量名，或启动时重复传 `--secret-env-name NAME`。

### OpenAI 兼容接口

OpenAI-compatible `/responses` 配置：

```bash
PICO_PROVIDER="openai"
PICO_MODEL="gpt-5.4"
PICO_BASE_URL="https://api.openai.com/v1"
PICO_API_KEY="your-openai-api-key"
```

也可以改成其他 OpenAI-compatible 服务：

```bash
PICO_PROVIDER="openai"
PICO_MODEL="gpt-5.4"
PICO_BASE_URL="https://your-api.example/v1"
PICO_API_KEY="your-api-key"
```

### Anthropic 兼容接口

Anthropic-compatible `/messages` 配置：

```bash
PICO_PROVIDER="anthropic"
PICO_MODEL="claude-sonnet-4-6"
PICO_BASE_URL="https://api.anthropic.com/v1"
PICO_API_KEY="your-anthropic-api-key"
```

### Ollama

如果要改用本地 Ollama，显式传 `--provider ollama`：

```bash
ollama serve
ollama pull qwen3.5:4b
uv run pico --provider ollama --model qwen3.5:4b
```

也可以在 `.env` 中配置 `PICO_PROVIDER=ollama`、`PICO_MODEL` 和 `PICO_BASE_URL`；Ollama 会忽略空的 `PICO_API_KEY`，但缺少 model 或 base URL 仍会在启动阶段报错。

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：查看提炼后的工作记忆
- `/session`：查看当前会话文件路径
- `/reset`：清空当前会话状态
- `/exit` 或 `/quit`：退出 REPL

## 安全与持久化

`pico` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

命令执行分成两条路径：`run_shell` 接收当前操作系统的 shell 命令字符串，适合管道和重定向；`run_process` 接收程序名和参数列表，不经过 shell，适合执行 Python、pytest、Git 等带参数的程序，也避免跨平台 quoting 差异。

- `--approval ask`
- `--approval auto`
- `--approval never`

每次运行结束后，都会在 `.pico/runs/<run_id>/` 下写出这些文件：

- `task_state.json`
- `trace.jsonl`
- `report.json`

这些内容默认只保存在本地，不需要跟仓库一起提交。

## 开发

常用本地检查：

```bash
uv run pytest tests -q
uv run ruff check pico tests scripts
```

内部代码现在按较轻的边界拆分：`pico/evaluation/` 放 benchmark 和 metrics，`pico/providers/` 放模型 provider client，`pico/features/` 放可选运行时能力。新代码应直接使用这些包路径；旧的 `pico.evaluator`、`pico.metrics`、`pico.models` 和 `pico.memory` import 不再作为公共入口保留。
