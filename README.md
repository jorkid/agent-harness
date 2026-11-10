# Agent Harness

Agent Harness 是一个最小可用的命令行 Agent。核心 Runtime 由项目自行实现，无需 LangGraph 等现成 Agent 框架。模型通过真实 DeepSeek API 决定直接回答或调用工具。内置工具包括 Calculator、Mock Search 和 Todo，支持待办的创建、查询、修改和删除。每个 Session 独立保存对话、待办和 Memory，可在重启后继续使用。

## 环境与安装

`pyproject.toml` 声明 Python 3.10 及以上。以下 Windows PowerShell 7 步骤使用已验证的 Python 3.12。请在项目根目录执行，并确认 PATH 中的 `python` 指向 Python 3.12。

```powershell
python --version
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -e .
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
notepad .env
```

| 配置 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 发送模型请求时必填。在本地 `.env` 填入有效密钥，或通过环境变量提供 |
| `DEEPSEEK_BASE_URL` | 模型服务地址，默认 `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型名，默认 `deepseek-flash` |

程序从当前目录读取 `.env`，已设置的环境变量优先。发送消息需要网络可达的模型服务和有效密钥。创建 Session 与直接查看本地 Todo 只需已安装项目及可访问的本地会话目录。Git 忽略本地配置、会话文件和运行时日志。[配置模板](.env.example)

## CLI 快速开始

```powershell
$sessionId = (python -m agent_harness.cli --new-session | ConvertFrom-Json).session_id
python -m agent_harness.cli --session $sessionId
python -m agent_harness.cli --session $sessionId "列出我的待办"
python -m agent_harness.cli --trace --session $sessionId "创建待办：写论文"
```

第一条命令创建 Session 并取得 ID。保存该 ID，重开终端后回到同一工作目录即可继续使用。省略消息时，CLI 直接读取该 Session 的 Todo。带消息时，模型决定如何处理请求。不同 Session 的新建、切换及 Todo 修改示例见 [CLI 演示](docs/cli-demo.md)。

| 场景 | stdout | 退出码 |
| --- | --- | --- |
| 创建 Session | JSON，包含 `session_id` | `0` |
| 读取 Session | JSON，包含 `session_id`、`todos` | `0` |
| 消息请求完成 | JSON，包含 `session_id`、`answer`、`status=completed` | `0` |
| 运行中失败或触顶 | 同样的 JSON 字段，`status` 为 `failed` 或 `limit_reached` | `1` |
| 会话、配置等入口错误 | 空，错误详情在 stderr | `1` |
| 命令用法错误 | 空，用法信息在 stderr | `2` |

普通模式的错误诊断写到 stderr。启用 `--trace` 时，结构化事件以 JSONL 写到 stderr，stdout 保持上表的格式。

## 系统架构

`CLI → Runtime → Model / Tool Manager / Session / Context`。Runtime 协调四个基础模块。

| 模块 | 职责 |
| --- | --- |
| CLI | 接收命令、消息与 Session ID，输出 JSON、Trace 和退出码 |
| Runtime | 加载会话，组织模型调用与工具执行，控制预算并保存本轮状态 |
| Model | 将消息和工具定义适配为 DeepSeek 请求，解析可见答复与工具调用 |
| Tool Manager | 管理 Registry，校验参数并分发到已注册的 handler |
| Session | 创建系统生成的 ID，隔离、读取并原子保存本地状态 |
| Context | 构造模型消息，按完整消息组管理近期历史、摘要和 Memory |

Registry 注册工具的名称、描述、参数 JSON Schema 和 handler，向模型提供前三项。Runtime 收到模型提出的调用后，Tool Manager 先校验通用 Schema，再执行 handler。Todo handler 还会按操作校验专属字段，Runtime 单独传入可信 Session ID。执行结果或错误以原工具调用 ID 配对回填给模型，模型可以继续决策，直至给出最终答复、发生不可恢复错误或达到预算。[架构设计](docs/02-design.md) · [已批准决策](docs/04-decisions.md)

## Session 与 Context

每个 Session 对应当前工作目录下的 `sessions/<ID>.json`。系统生成 ID，同一工作目录中使用原 ID 可在重启后恢复原会话。

| 字段 | 内容 |
| --- | --- |
| `messages` | 可回放的用户、助手与工具消息，压缩后主要保留近期窗口 |
| `todos` | 当前 Session 的待办与稳定 Todo ID |
| `memory` | 被选中的历史用户原话 |
| `summary` | 较早历史与已有摘要合并后的滚动摘要 |

写入时先在同目录生成临时文件，刷新并调用 `fsync`，再用 `os.replace` 原子替换。全量更新会比较预期快照与当前文件状态，历史追加检查已有消息是否变化。单个 Runtime 实例通过锁串行处理持久化会话请求。

### Memory 的提取、保存与召回

1. **压缩时机**。请求完成且新历史已保存后，若历史超过 20 条消息、单次请求还剩至少两次模型调用预算，就尝试压缩一批较早历史。目标是保留至少最近 10 条消息，以保留追问所需的近期原文。带工具调用的 assistant 消息和对应的全部 tool 结果一起保留或移出，按原调用 ID 和顺序校验。较早历史按预算分批处理，暂时放不下的完整组保留原文。
2. **提取与保存**。第一次维护调用从已有 Memory 和本批较早的简短用户原话中选择索引，Context 校验索引并保存对应原句。第二次调用把较早消息与旧 Summary 合并。两次均通过校验后，Runtime 在一次带快照检查的保存中提交新 Memory、Summary 和保留的消息。提取或摘要失败时原始历史仍在磁盘上。快照冲突时保留磁盘当前状态，保存异常时读回核查。
3. **召回与注入**。每次用户请求先加载当前 Session。Context 将 Summary 和 Memory 作为 JSON 加到首条系统消息内，分别标记为未经核实的摘要和历史用户原话，随后拼接预算内的近期消息组及当前用户消息。同一请求内的后续决策沿用这份上下文，并追加本轮工具调用和结果。召回采用同 Session 直接读取。
4. **长度保护**。摘要最多 2,000 字符，Memory 最多 16 条、每条 240 字符。用户输入上限为 8,000 字符，模型消息列表采用 64,000 序列化字符预算。超长工具结果转换为有界 JSON，超长列表保留完整条目的预览和截断标记。工具消息和最终答复上限均为 8,000 字符。

## 执行限制与异常

每次用户请求最多调用模型 8 次，包括 Context 的提取和摘要调用。第 8 次返回最终答复时可正常完成，仍请求工具时跳过该批执行并返回 `limit_reached`。模型调用失败、历史保存无法确认等情况会返回 `failed`。

Todo 写入后，Runtime 读回会话 JSON 核查。确认已写入时可以继续，确认原状态时报告失败。读回失败或状态无法确认时返回 `tool_state_uncertain`，跳过同批剩余工具并停止请求。此前已确认的写入仍可通过无消息的 `--session` 命令查到。部分工具成功后的 `failed` 或 `limit_reached` 描述本次请求的最终状态。

## Trace 与测试

`--trace` 放在 `--session` 之前。用 `request_id` 和 `sequence` 定位同一请求内的事件，用 `tool_call_id` 关联 `tool_requested`、`tool_started` 和 `tool_result`。结合工具结果的状态、`error_code` 和 `request_finished` 可以核对工具实际执行、校验拒绝、跳过与请求最终状态。Trace 对参数和结果只保留必要摘要。

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

自动化套件使用确定性模型测试工具、状态、跨进程恢复、故障和预算。T11 的真实 DeepSeek 探测另行验证了 Calculator 得到结果、Todo 创建和重启后查询。真实探测使用配置的 API，会产生模型调用费用。[测试报告与真实探测摘要](docs/07-test-report.md)

## 已知限制

- 真实 DeepSeek 连续第 100 轮的端到端效果尚未验证。现有第 100 轮结果来自确定性模型。
- 全新环境在线安装尚未验证。已有依赖下的可编辑安装和 CLI 冒烟通过。
- 多实例或多进程并发写入共享目录超出当前保证范围。
- 同步工具持续阻塞时缺少统一硬超时。
- 自然语言有歧义时，模型可能选错当前 Session 内合法的 Todo ID。
- Mock Search 查询固定模拟数据，结果带 `mock: true`。
- Trace 默认只写到输出流，需显式重定向才能留档。

## 架构设计笔试

除最小可用 Agent 实现外，笔试还包含五个独立架构设计问题：

[查看五道架构设计题解](ARCHITECTURE-DESIGN-ANSWERS.md)

## 文档索引

- [需求规格](docs/01-requirements.md) · [系统架构](docs/02-design.md) · [任务与验收标准](docs/03-tasks.md) · [架构决策](docs/04-decisions.md)
- [CLI 演示](docs/cli-demo.md) · [综合测试报告](docs/07-test-report.md) · [AI 使用与问题解决记录](docs/06-ai-usage.md) · [开发日志](docs/logs/development.md)
