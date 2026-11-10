# T11 综合测试与需求覆盖报告

验收日期为 2026-11-11，基线为 `5a4f08d`。本地环境为 Windows、Python 3.12.0。CLI 集成用例、安装检查和真实探测分别使用临时目录。模型故障用例只替换模型边界，CLI、Runtime、工具执行和 Session JSON 走实际实现。

## 执行记录

| 编号 | 执行命令 | 实际结果 | 证据 |
| --- | --- | --- | --- |
| A | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` | 基线 133 项通过，92.444 秒。修复后 135 项通过，89.735 秒。两次退出码均为 0 | 确定性测试，包含独立 CLI 进程 |
| B | `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -k hundredth_turn -v` | 1 项通过，2.606 秒，退出码 0 | 确定性模型与实际 Session JSON |
| C | `.\.venv\Scripts\python.exe -B tests/live_deepseek_probe.py` | 通过，退出码 0。两个 CLI 请求分别消耗 3 和 2 次模型决策 | 真实 DeepSeek API、真实工具执行、Trace 与磁盘 |
| D | `.\.venv\Scripts\python.exe -B -` 运行只读扫描，调用 `git ls-files`、`git check-ignore --no-index`、`git show :<path>`、`git rev-list --objects HEAD` 和 `git cat-file` | 配置中的密钥、常见凭据模式和非空凭据赋值扫描均为 0 命中。禁止框架的导入及声明均为 0 | 静态检查 |
| E | 临时项目副本执行 `python -m venv`、`python -m pip install -e .`、`python -m pip check` 和 `python -B -m agent_harness.cli --new-session` | 全新环境的两次在线安装分别在 105 秒和 45 秒后超时。使用现有依赖在临时副本执行 `pip install --no-build-isolation --no-deps --no-index --target <临时目录> -e .`、`pip check` 及已安装 CLI 冒烟均通过，退出码 0 | 临时目录安装检查 |
| F | `git diff --check` | 通过，退出码 0 | 静态检查 |

## 需求覆盖矩阵

| 需求 | 源码或测试位置 | 命令和证据类型 | 实际结果 | 结论与未验证范围 |
| --- | --- | --- | --- | --- |
| FR-01 模型自主工具调用及 Agent Loop | [Runtime](../src/agent_harness/runtime.py)、[Model](../src/agent_harness/model.py)、[真实探测](../tests/live_deepseek_probe.py) | A 确定性，C 真实 LLM | Calculator 返回 120，下一次模型决策请求 Todo 创建，回填后模型返回最终答案 | 通过。其他模型供应商未验证 |
| FR-02 工具注册、Schema 和非法调用 | [Registry](../src/agent_harness/tools/registry.py)、[通用 Schema 拒绝](../tests/test_fault_trace.py#L131)、[Todo 通用 Schema 用例](../tests/test_todo.py#L108)、[Todo 操作专属用例](../tests/test_todo.py#L99) | A 确定性，C 真实 LLM 合法调用 | Registry 在 handler 启动前拒绝未知工具、无效 Schema 或不符合通用 Schema 的参数。通过通用 Schema 的 Todo 参数还会在 handler 内按操作校验。缺少 create 的 content、为 list 附加 content 或缺少 delete 的 todo_id 时返回 `invalid_arguments`。两阶段的拒绝均发生在修改 Todo 状态前 | 通过。用例确认通用 Schema 拒绝时 handler 未启动，并核对两类 Todo 参数错误均未写入待办 |
| FR-03 三工具及 Todo CRUD | [工具实现](../src/agent_harness/tools/)、[工具测试](../tests/test_tools.py)、[Todo 测试](../tests/test_todo.py)、[会话测试](../tests/test_session.py) | A 确定性，C 真实 LLM | Calculator、Todo 创建及查询在真实探测执行。Todo 修改删除、边界及 Mock Search 固定结果由自动化覆盖 | 通过。Mock Search 使用模拟数据 |
| FR-04 A/B 隔离与跨进程恢复 | [SessionStore](../src/agent_harness/session.py)、[独立进程用例](../tests/test_session.py#L289) | A 确定性，C 真实 LLM 单会话恢复 | A/B 各自保留原 Todo ID。A 修改和删除后 B 的完整快照保持一致 | 通过。多进程顺序恢复已覆盖 |
| FR-05 普通及工具追问、消息配对 | [Context](../src/agent_harness/context.py)、[会话测试](../tests/test_session.py#L397) | A 确定性，C 真实 LLM，T07 真实记录 | 同 Session 普通追问及 Todo 追问可回放。真实工具请求与持久化结果按 ID 配对 | 通过。泛化到任意歧义表达未验证 |
| FR-06 滚动压缩、Memory 及长历史 | [Context](../src/agent_harness/context.py)、[压缩测试](../tests/test_compaction.py) | A/B 确定性，T08 真实合成召回记录 | 超过 20 条触发压缩，工具组完整，长积压分批缩减，跨进程恢复摘要和 Memory。第 100 轮测试输出 `3.50` | 基础压缩及确定性第 100 轮通过。真实模型连续 100 轮未验证 |
| FR-07 单请求 8 次决策 | [Runtime](../src/agent_harness/runtime.py)、[预算与故障测试](../tests/test_fault_trace.py) | A 确定性 | 第 8 次返回最终答案可以完成，第 8 次请求工具则跳过并报告 `limit_reached`。维护调用计入同一预算 | 通过。真实模型持续工具请求未强制复现 |
| FR-08 基础异常处理 | [Runtime](../src/agent_harness/runtime.py)、[故障测试](../tests/test_fault_trace.py)、[存储故障测试](../tests/test_session.py) | A 确定性 | 模型超时、工具错误、替换前后异常、读回失败及压缩冲突均有受控结果。已确认 Todo 保持可恢复 | 通过。同步阻塞 handler 仍需自身报告超时 |
| FR-09 Trace 可关联且脱敏 | [Trace](../src/agent_harness/trace.py)、[CLI](../src/agent_harness/cli.py)、[故障测试](../tests/test_fault_trace.py) | A 确定性，C 真实 LLM，D 静态 | 成功工具的 proposed、started、result 同一调用可关联，拒绝和跳过均可区分。失败 stderr 逐行合法 JSON | 通过。默认 Trace 只输出至日志流 |
| NFR-01 自实现 Runtime | [Runtime](../src/agent_harness/runtime.py)、[依赖声明](../pyproject.toml) | D 静态 | AST 导入和依赖声明均无禁止框架 | 通过 |
| NFR-02 真实 LLM 集成 | [Model](../src/agent_harness/model.py)、[真实探测](../tests/live_deepseek_probe.py) | C 真实 LLM | 实际 `deepseek-flash` 完成 Calculator、Todo 和重新打开会话后的查询 | 通过。其他模型供应商未验证 |
| NFR-03 隔离与一致性 | [SessionStore](../src/agent_harness/session.py)、[会话测试](../tests/test_session.py)、[Todo 测试](../tests/test_todo.py) | A 确定性，C 真实 LLM 单会话 | 稳定 ID、会话内写入、故障后读回和历史快照一致 | 通过。共享目录多进程并发写入未验收 |
| NFR-04 秘密保护 | [Trace](../src/agent_harness/trace.py)、[忽略规则](../.gitignore)、[示例配置](../.env.example) | A 确定性，D 静态 | 无配置密钥命中，无真实 `.env`、会话或运行时日志纳入 Git。未验证的 CLI 会话标识已脱敏 | 扫描和回归通过。扫描范围见下节 |
| NFR-05 有界执行与故障可观察性 | [Runtime](../src/agent_harness/runtime.py)、[Context](../src/agent_harness/context.py)、[故障测试](../tests/test_fault_trace.py) | A/B 确定性，C 真实 LLM | 8 次决策、上下文长度、维护预算和最终状态均有断言及 Trace | 通过。同步工具硬超时未验收 |

## 真实 DeepSeek 与 CLI 证据

2026-11-11 的 T11 探测通过官方 DeepSeek HTTPS 端点运行真实模型 `deepseek-flash`，退出码为 0。该次探测在会话标识脱敏修复前完成，修复后的全量回归验证了有效会话的 Trace 契约。创建会话后，两条带消息的命令各启动一个 CLI 进程，第二条沿用第一个 Session ID。输出只保留合成数据及核查结果。

| 请求 | 模型决策次数 | Trace 中实际成功的工具 | 磁盘与回答 |
| --- | --- | --- | --- |
| 计算并创建 | 3 | 第 1 次决策 `calculator/call-1-1`，第 2 次决策 `todo/call-2-1`。两项均有请求、启动及成功结果 | Calculator 回填 `120`。权威 JSON 保存一条 `T11-120`，首个回答的 value 和 Todo ID 与磁盘匹配。6 条历史消息 |
| 重启后询问刚才的待办 | 2 | 第 1 次决策 `todo/call-1-1`，有请求、启动及成功结果 | Todo 工具返回同一条记录，最终回答的内容和 ID 与磁盘匹配，原 6 条消息保持并追加到 10 条 |

### 已保存的脱敏运行摘要

以下 JSON 来自原次探测的 stdout，仅调整缩进。探测在临时会话中读取并断言原始 Trace 和磁盘状态，留存的是运行摘要。摘要中的 `call-1-1` 等值是 Runtime 生成的请求内 Trace 标识，同一值可以出现在两个请求中。相关依据见 [T11 开发记录](logs/development.md#L188)、[Trace 断言](../tests/live_deepseek_probe.py#L59)和[摘要输出](../tests/live_deepseek_probe.py#L143)。

```json
{
  "status": "passed",
  "model": "deepseek-flash",
  "calculator": 120,
  "todo": {
    "content": "T11-120",
    "id_consistent": true
  },
  "first": {
    "decisions": 3,
    "tools": [
      {
        "id": "call-1-1",
        "name": "calculator",
        "decision": 1,
        "status": "succeeded"
      },
      {
        "id": "call-2-1",
        "name": "todo",
        "decision": 2,
        "status": "succeeded"
      }
    ]
  },
  "followup": {
    "decisions": 2,
    "tools": [
      {
        "id": "call-1-1",
        "name": "todo",
        "decision": 1,
        "status": "succeeded"
      }
    ]
  },
  "persisted_messages": [
    6,
    10
  ]
}
```

`id_consistent: true` 表示探测核对了创建结果、创建后的磁盘 Todo、恢复后的列表结果和最终磁盘 Todo 的 ID 一致。两次最终回答的 `todo_id` 也等于该 ID。首答的 `value` 为 `120`，追问答复的 `content` 为 `T11-120`，两次答复原文都与各自落盘的最后一条消息一致。探测同时按协议 ID 配对了历史里的 assistant 工具调用和 tool 消息。核对逻辑见 [创建断言](../tests/live_deepseek_probe.py#L120)和[恢复查询断言](../tests/live_deepseek_probe.py#L138)。

- 2026-11-10 的 T05 用真实 DeepSeek 连续执行 `1+1` 和 Todo 创建，内存状态和最终回答均为 `2`。覆盖工具连用与回填。
- 2026-11-10 的 T07 用真实 DeepSeek 在恢复后答出前文的 `22:47`，并对“把刚才那个改到明天”更新同一个 Todo。覆盖普通追问与工具追问。初次 Todo 更新探测失败，UTF-8 复测通过。
- 2026-11-10 的 T08 用真实 DeepSeek 从加入合成历史的 Session 提取和召回约定，另用仅含 Memory 的隔离样例复查。覆盖合成召回，真实连续 100 轮未验证。

以上历史证据及执行细节见 [开发日志](logs/development.md)。

## 独立进程验收及故障

- `test_cli_round_trip_through_independent_processes` 新建 A/B，分别创建“写论文”和“买牛奶”。恢复后原 ID 均保持。A 修改后仍用原 ID，删除后为空。B 的完整快照保持一致。压缩后的 A 恢复请求把原 Todo 工具结果、摘要和 Memory 注入模型输入。
- `test_cli_reports_model_error_after_confirmed_todo` 与 `test_cli_reports_budget_exhaustion_after_confirmed_todo` 在独立进程核对 JSON stdout、逐行 JSON Trace 和退出码 1。对应状态分别为 `failed`、`limit_reached`，两者均保留已确认的 Todo，触顶后请求的额外 Todo 未写入。
- 正常 CLI 退出码 0。缺失 Session 退出码 1，stdout 为空。无效 Trace 命令退出码 2，stderr 为单条 JSON。实际 CLI 创建、读取、发消息和 Trace 的用法见 [CLI 演示](cli-demo.md)。
- 故障注入验证了存储替换前失败、替换后读回、读回不确定、压缩快照冲突，以及未知工具、参数校验和模型超时。用例分别核对错误码、阶段与磁盘状态。

## 安全、依赖与安装

- 路径检查涵盖索引、可纳入 Git 的当前工作树以及忽略规则。`.env`、`sessions/`、根运行时日志目录及 `*.log` 均命中忽略规则。已跟踪的 `docs/logs/` 是工程记录。
- 内容检查对索引、当前工作树和 HEAD 可达的 9 个提交、77 个去重 blob 扫描。检查实际配置密钥的字节匹配、常见 token 模式和非空凭据赋值，只输出计数。三类均为 0 命中。README 与 `.env.example` 在扫描范围内。
- 直接依赖为 `openai`、`python-dotenv` 和 `jsonschema`。AST 与依赖声明均未发现 LangGraph、LangChain、OpenHands 或 OpenClaw。现有依赖可完成本地可编辑安装及 CLI 冒烟。全新环境在线下载依赖未验证。
- Trace 的工具名来自可信注册表，未知名使用固定标签，模型工具 ID 映射为请求内生成值。用户文本与参数原文保留在需要它们的会话消息中，Trace 只记录必要的类型、长度、结果和状态。

## 缺陷、未验证项与 T12 交接

| 级别 | 项目 | 状态 |
| --- | --- | --- |
| P2 | CLI 失败 Trace 原先会写入未验证的会话标识。使用两个合成标识复现，包括符合 32 位十六进制格式但不存在的 ID | 已修复。加载有效 Session 后才将 ID 写入 Trace。加载前失败用脱敏值，成功请求的 ID 和三阶段 Trace 由回归确认 |
| P2 | 真实模型第 100 轮留存 | 尚未验证。T11 的第 100 轮是确定性模型配合真实压缩、磁盘及 Calculator，T08 提供另外的真实合成召回证据 |
| P2 | 全新虚拟环境在线安装 | 两次安装超时。本地可编辑安装、依赖一致性及 CLI 冒烟通过，在线下载路径待验证 |
| P3 | 同步工具硬超时、共享目录多进程并发写入 | 当前边界。本轮验证顺序恢复和显式工具超时 |

P0/P1 待修项为 0。T12 可引用本报告的覆盖矩阵、真实调用证据和 CLI 演示，注明真实模型第 100 轮仍待验证。T11 等待独立审查。
