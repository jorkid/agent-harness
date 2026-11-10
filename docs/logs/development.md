# 开发日志

> 记录开发过程中的实现细节、变更与提交。

## 2026-11-08

- 初始化项目，建立文档骨架。

## 2026-11-10（T01：项目骨架、环境配置与 DeepSeek 真实 LLM 接入）

- **目标**：最简 Python 包骨架 + 配置读取 + DeepSeek 纯文本请求，不实现 Loop/工具/Session/Context。
- **新增文件**：`pyproject.toml`、`.env.example`、`.gitignore`、`src/agent_harness/{__init__,config,model,cli}.py`。
- **实现内容**：setuptools src 布局 + `pip install -e .`；`config.py` 读 `.env`/环境变量（缺 KEY 抛 ConfigError，不泄密）；`model.py` 用 `openai.OpenAI(base_url=…)` 发纯文本 chat，异常转脱敏 `ModelError`；`cli.py` 提供 `python -m agent_harness.cli "<消息>"` 入口。
- **遇到的问题**：PyPI（pypi.org）直连 read timeout，构建隔离阶段拉取 setuptools 失败。
- **采用的方案**：改用清华镜像 `https://pypi.tuna.tsinghua.edu.cn/simple` + `--no-build-isolation` 完成安装。
- **实际验证结果**：
  - venv 创建成功（Python 3.12.0）。
  - `pip install -e .` 成功（agent-harness 0.1.0、openai 3.48.0、python-dotenv 1.2.4）。
  - 缺 KEY：退出码 1，报「缺少 DEEPSEEK_API_KEY…」。
  - 真实请求 `python -m agent_harness.cli "Hello"`：模型 `deepseek-flash` 返回「Hello! How can I help you today?」，退出码 0。
  - `git check-ignore -v .env` 命中；`git ls-files` 确认 `.env` 未跟踪、无 `sk-` 密钥串。
- **尚未解决**：无阻塞问题；`deepseek-flash` 经实测可用（此前担心的模型名风险解除）。

## 2026-11-10（T02 工具注册、Schema 校验与基础工具）

- **任务范围**为 FR-02、FR-03a、FR-03b。新增工具包和单元测试，补充依赖声明。T01 的 Model、CLI、配置源码未改动。
- **接口**为 `ToolDefinition(name, description, parameters_schema, handler)` 和 `ToolManager.register`、`get_definition`、`public_definitions`、`execute`。用 `create_default_tool_manager()` 注册 Calculator 和 Mock Search。执行结果为 `{"ok": true, "result": {...}}` 或 `{"ok": false, "error": {"code": "...", "message": "..."}}`。
- **Schema**使用 `jsonschema>=4.18,<5` 的 Draft 2020-12 验证器。注册时检查 Schema 并保存快照，执行前拒绝非法 JSON 对象或不符合 Schema 的参数，公开定义只包含名称、描述和 Schema。选用库以便直接复用同一份 Schema。无外部依赖的备选是手写当前关键字的子集，但需要维护支持范围以及拒绝未知关键字的规则。
- **Calculator**用 AST 白名单支持十进制四则运算、正负号和括号。表达式上限 256 字符，字面量和中间值绝对值上限为 10¹⁵。区分非法表达式、除零和数值越界。
- **Mock Search**对关键词去首尾空白并做大小写折叠。内置 `python` 和 `agent harness` 两条记录。查询上限 128 字符，合法但无匹配的查询返回空列表。每次成功结果都带 `mock: true`。
- **遇到的问题与处理**包括 Git 对仓库所有权的检查。改用命令级 `git -c safe.directory=D:/Software/project/agent_harness ...` 完成状态与 diff 核查。旧文档的进度汇总仍有 T01 待开始的描述，本轮以交接、T01 日志和本地提交为准。
- **实测环境**为项目原有的 Windows 虚拟环境，Python 3.12.0、jsonschema 4.26.0。安装命令 `.\.venv\Scripts\python.exe -m pip --disable-pip-version-check install --no-build-isolation --index-url https://pypi.tuna.tsinghua.edu.cn/simple --timeout 15 --retries 1 -e .` 成功。
- **实测命令**为 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`。首轮运行 20 项，耗时 0.115 秒，全部通过。另执行 `.\.venv\Scripts\python.exe -m pip check`，无依赖冲突。
- **DoD 证据**来自单测及本地工具调用。合法参数分发到注册的 handler，未知工具、缺必填字段、类型错误和其他非法参数均被拒绝，公开定义通过 JSON 序列化且没有 handler。计算器通过优先级、括号、除零、恶意表达式和越界测试。Mock Search 命中固定数据时有模拟标记。
- **T03 交接**。从 `public_definitions()` 取得中立定义，将 `parameters_schema` 映射为模型接口的 `function.parameters`。Model 解析模型输出中的 JSON 对象并保留工具名及调用 ID。T04 的 Runtime 负责分发及关联结果。两个工具均无状态，额外的 `session_id` 参数会被拒绝。真实模型对工具 Schema 的兼容性留待 T03 验证。
- **首轮交付**。提交给项目负责人和独立 Code Review 核对。

### 最终修复与验收

- **Calculator** 从原始字面量用标准库 `Decimal` 解析，用 `Fraction` 精确检查字面量和中间值。超过绝对值上限或转换为浮点数时会变成零的非零值返回 `out_of_range`，零除数返回 `division_by_zero`。
- **Schema 与工具结果**。校验失败返回结构化错误并阻止 handler 执行。注册时拒绝 `multipleOf` 和外部引用，引用解析关闭自动联网。同文档引用及基础校验继续可用。handler 返回值须通过严格 JSON 序列化检查，非法返回值报 `invalid_tool_result`。
- **最终验证**。在 Python 3.12.0、jsonschema 4.26.0 环境运行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，29 项全部通过，耗时 0.148 秒，退出码 0。覆盖外部引用与 `multipleOf` 的注册拒绝、同文档引用和原有工具行为。
- **已知限制**。注册检查扫描整个 Schema 文档，包含受限关键字形状的示例数据也可能被拒绝。缺失的同文档引用目标在执行时返回 `invalid_tool_schema`。Calculator 的成功结果以 `int` 或 `float` 输出，小数最终转换可能舍入。真实模型的工具调用兼容性由 T03 验证。
- **验收**。独立 Code Review 最终通过，项目负责人批准 T02 收尾。

## 2026-11-10（T03 Model 工具协议适配与调用解析）

- **起点**。核对 HEAD 为 `9d60cd5`，T01 和 T02 已本地提交。保持已批准的 Model 与 Tool Manager 职责边界，原有 `Model.chat(text)` 继续作为 T01 CLI 的入口。
- **实现**。`Model.decide(messages, tool_definitions=())` 发送一次 Chat Completions 请求。将公开定义中的 `name`、`description` 和 `parameters_schema` 映射为 API 的 `type: "function"` 和 `function` 字段。采用普通工具模式，显式关闭 thinking，按可见消息构造回填数据。请求格式依据 [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)。
- **响应与错误**。`kind: "final"` 携带最终 `text`。 `kind: "tool_calls"` 携带按响应顺序排列的 `tool_calls` 和 `assistant_message`。每条调用包含 `tool_call_id`、`name`、解码后的 JSON 对象 `arguments`。assistant 消息保留可见 `content` 及原始参数字符串。空回复、缺字段、重复调用 ID、畸形或非对象参数、不支持的协议及请求失败返回受控 `ModelError`。
- **确定性测试**。在 Python 3.12.0、OpenAI SDK 3.48.0 环境执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`。新增 12 项 Model 测试，完整测试共 41 项通过，耗时 0.148 秒，退出码 0。覆盖最终文本、单个和多个工具调用、异常结构、Schema 转换、可见消息重放数据和 T01 入口兼容性。
- **真实 API 验证**。使用现有配置，执行 PowerShell `$probe | .\.venv\Scripts\python.exe -B -`。脚本用 `Model.decide` 各发送一次合成文本和工具提示，命令退出码为 0。保留通过白名单检查的字段如下，两个请求都使用 `deepseek-flash`。
  - 普通回复。提示 `Reply with exactly T03_OK and no punctuation.`，结果 `{"kind":"final","text":"T03_OK"}`。
  - 工具请求。传入 Calculator / Mock Search 的公开定义。提示 `Use the calculator tool exactly once with the expression 17*(23+5).`，结果 `{"kind":"tool_calls","tool_call_id":"call_00_YrGMcYgbb1LXwzNBW3Y33883","name":"calculator","arguments":{"expression":"17*(23+5)"},"executed":false}`。同时核对了 assistant 消息中的 ID 及原始参数字符串与解析结果一致。
- **T04 交接**。Runtime 先原样追加 `assistant_message`，再按 `tool_calls` 顺序将工具名与解码参数交给 Tool Manager。为每条调用追加带匹配 `tool_call_id` 的 `role: "tool"` 消息，`content` 使用工具结果或错误的 JSON 字符串。解析错误抛出 `ModelError`，整次响应没有可分发的部分结果。
- **最终验收**。独立 Code Review 已通过，项目负责人批准 T03 收尾。真实工具执行和结果回填后的下一轮请求属于 T04 验证。
- **收尾复测**。执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，41 项全部通过，耗时 0.164 秒，退出码 0。
- **SDK 自动重试风险**。本地 OpenAI SDK 3.48.0 默认 `max_retries=2`，当前 Model 沿用该设置。遇到可重试的网络错误或 HTTP 响应时，一次 `Model.decide()` 最多可能发起三次 HTTP 尝试。Runtime 记录的模型决策次数与 HTTP 尝试次数可能不同，重试也可能增加延迟及服务用量。

## 2026-11-10（T04 自主 Agent Loop 与最小 Trace）

- **起点与范围**。基于已提交的 T03（`bccd895`）。Runtime 以 `Runtime(model, tools).run(message, session_id)` 为入口，用请求内的 Messages 协调 Model 和 Calculator / Mock Search。结果包含 `session_id`、`answer` 和 `status`。
- **循环与预算**。每次 `Model.decide()` 消耗一次预算。收到工具批次时先原样追加 assistant 消息，再顺序追加按 ID 配对的 JSON 工具结果或错误。第 8 次若为最终回答，则返回 `completed`；若仍请求工具，则按已批准设计停止新工具执行，为每个调用 ID 记录未启动的预算错误，并返回 `limit_reached`。模型抛出 `ModelError` 时返回 `failed`。
- **执行状态与 Trace**。Tool Manager 新增 `execute_with_start_status(name, arguments)`，返回实际 handler 启动标记和既有结果结构。原有 `execute()` 契约保留。Runtime 通过 `agent_harness.runtime` 的 INFO 日志输出 JSON Trace，包含可信会话标识、调用 ID、工具名、开始状态、成功状态、结果摘要或错误码。Trace 省略原始参数及自由文本，并遮盖常见 `sk-` 形态。
- **SDK 配置**。按已批准决策设 `max_retries=0`、`timeout=30.0`。通过本地测试与真实请求前的客户端检查核实两个实际值。
- **本地验证**。运行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`。50 项全部通过，耗时 0.487 秒，退出码 0。新增 9 项，覆盖直接回复、真实工具在模拟模型循环中的执行和回填、错误与拒绝、批次顺序、第 8 次预算边界、SDK 超时与配置，以及最小脱敏。
- **真实闭环验证**。以 PowerShell `$probe | .\.venv\Scripts\python.exe -B -` 运行观察脚本，现有配置使用 `deepseek-flash`，退出码 0。合成提示要求调用 Calculator 计算 `17*(23+5)`，收到工具结果后只回复数字。模型调用共两次。
  - 首次真实响应 `kind="tool_calls"`，工具 `calculator`，ID `call_00_ffdJnf6VsTABZQvQTDOL5059`，参数 `{"expression":"17*(23+5)"}`。
  - 真实 Runtime Trace 记录同 ID，`session_id="t04-live-calculator"`，`started=true`、`ok=true`，摘要 `{"field_count":1,"value":476}`。第二次请求确认原样保留 assistant 消息，并回填同 ID 的 `role="tool"` 消息，结果 `{"ok":true,"result":{"value":476}}`。
  - 第二次真实响应为 `final`，Runtime 返回 `{"session_id":"t04-live-calculator","answer":"476","status":"completed"}`。
- **后续对接与边界**。T05 可沿执行边界从 Runtime 单独接收可信 session_id。T06 可将每次请求构建和保存会话状态的职责接到 Runtime 外围。本轮仅使用请求内消息，Trace 由调用方启用 INFO 接收。持久化、会话恢复、完整 Trace 与跨轮历史留给各自任务。
- **验收状态**。T04 DoD 已取得本地与真实闭环验证证据，待独立 Code Review。
- **最终验收**。独立 Code Review 已通过，项目负责人批准 T04 收尾。
- **剩余风险**。Trace 目前由调用方启用 INFO 日志接收，尚未持久化到文件（完整 Trace 留待 T09）；会话历史仅存在于单次请求的 Messages 内，跨轮记忆与 Session 恢复留待 T05–T07；真实闭环验证仅覆盖 Calculator，Mock Search 及多工具连用的真实端到端验证留待后续任务。

## 2026-11-10（T05 Todo CRUD 与可信 Session 注入）

- **起点与范围**。开始时 HEAD 为 `bccd895`，T04 已验收且改动仍在工作树。收尾核查时 T04 已有本地提交 `c78bcaf`，T05 改动仍在工作树。本轮保留 T04 的 Model 配置、预算规则和测试。
- **内存存储与工具**。`TodoStore` 在单进程内按可信会话归属管理 `{id, content}`，用 UUID 分配稳定 ID。工具 `todo` 支持 `create`、`list`、`get`、`update`、`delete`。按 ID 操作同时校验记录的会话归属，跨会话和不存在的 ID 均返回 `todo_not_found`。注册方式为 `manager.register(create_todo_tool(store))`。
- **可信注入**。Runtime 将 `session_id` 作为工具执行接口的独立关键字参数传入。Tool Manager 只向声明 `requires_session` 的 handler 注入。Todo Schema 无会话字段并拒绝额外参数。原有无状态工具调用方式及结果格式可继续使用。
- **写入与重复保护**。同一会话中重复创建去除首尾空白后的相同活跃内容时，返回原 ID 和 `created=false`。更新到其他活跃记录的同一内容会被拒绝。存储在同一把锁下写入并核查异常后的目标状态，核实成功返回 `recovered=true`。无法确认的异常或非法工具结果返回 `tool_state_uncertain`，Runtime 跳过同批剩余调用并停止本次请求。模型失败或预算触顶保留已写入状态与 Trace。
- **本地验证**。执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，60 项全部通过，耗时 0.556 秒，退出码 0。新增 10 项，覆盖 CRUD、ID 和会话隔离、伪造会话参数、重复创建和 UUID 冲突、模型异常和预算后的状态，以及写后抛错与不可序列化结果。故障注入分别验证核实后复用原 ID 和无法核实时停止两条路径。
- **真实 DeepSeek 闭环**。用 PowerShell `$probe | .\.venv\Scripts\python.exe -B -` 执行观察脚本，退出码 0。使用 `deepseek-flash`，合成提示要求先计算 `1+1`，收到 Calculator 结果后用 Todo 创建字符串内容为计算结果的待办，共三次模型决策。
  - `calculator` 调用 ID `call_00_5uAoSGtq7eQ8yEuPmrqP0115`，参数 `{"expression":"1+1"}`，工具回填结果为 `2`。下一轮模型收到该工具消息后请求 `todo`。
  - `todo` 调用 ID `call_00_fzerHCe8XybsV9GjM5B92331`，参数 `{"action":"create","content":"2"}`。两条 Trace 均关联 `t05-live-A`，同各自调用 ID，`started=true`、`ok=true`。
  - `TodoStore.list("t05-live-A")` 实测返回 `[{"id":"27d0d9d80b10477db2f0e93053391bc7","content":"2"}]`，与回填的 Todo 结果一致。对照会话 `t05-live-B` 为空。Runtime 返回 `completed`，最终回答为 `Created todo 2`。
- **T06 交接与限制**。复用可信会话标识与 `{id, content}` 记录结构。持久化存储需沿用会话范围内的 CRUD 接口和写后核查语义。当前状态只存在于同一个 TodoStore 实例，去重限于当前活跃的同内容记录。会话持久化、重启恢复与对话历史均留给各自任务。
- **最终验收**。独立 Code Review 已通过，项目负责人批准 T05 收尾。本地复测 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，60 项全部通过（OK，退出码 0）。
- **三个已知问题**：
  1. **内存态无持久化**：Todo 状态只存在于单个 `TodoStore` 实例，进程重启即丢失；会话持久化与重启恢复留待 T06。
  2. **去重仅限活跃记录**：`create` 去重只对当前未删除的同内容记录生效，删除后重建相同内容会生成新 ID。
  3. **无跨轮对话历史**：每次请求的 `messages` 均由单条用户消息新建，Runtime 不保存历史，跨轮记忆留待 T06–T07。

## 2026-11-10（T06 Session JSON 隔离、创建与重启恢复）

- **范围**。开始时 HEAD 为 `c78bcaf`，已验收的 T05 仍在工作树。本轮保留其可信 Session 注入、Todo ID、去重和异常语义。
- **会话文件**。`SessionStore(directory)` 提供 `create()`、`load(session_id)` 和 `save(session_id, state, expected=原始快照)`。创建时生成 32 位小写十六进制 UUID，文件包含 `messages`、`todos`、`memory` 和 `summary`。加载与保存拒绝非法 ID、缺失文件、符号链接或硬链接别名及无效状态。
- **原子写入**。在目标目录写完整临时 JSON，执行 `flush()`、`os.fsync()`，关闭句柄后用 `os.replace()` 替换，最后清理临时文件。保存要求目标会话已存在。
- **Todo 对接**。Runtime 加载权威 JSON，按可信会话 ID 恢复已有 Todo。每次变更先构建候选快照，Runtime 保存后读回整个 JSON，确认与候选一致才更新内存并报告成功。异常后若文件等于候选则报告 `recovered=true`；若仍是旧状态则返回 `todo_save_failed`；读回失败或状态不符则返回 `tool_state_uncertain` 并停止本次请求。后续模型异常不影响此前已核实的写入。
- **CLI**。在仓库根目录，`python -m agent_harness.cli --new-session` 创建会话，`python -m agent_harness.cli --session <ID>` 选择并查看待办，`python -m agent_harness.cli --session <ID> "<消息>"` 恢复并执行请求，输出 Session ID、答案和状态。保留纯文本调用。默认数据目录为当前目录下的 `sessions/`，沿用已有 Git 忽略规则。
- **实际验证**。接入后原有 60 项测试通过。执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，71 项全部通过，耗时 30.868 秒，退出码 0，新增 11 项。
  - 跨进程 CLI 测试使用确定性模拟模型，实际执行 CLI、Runtime、Tool Manager 和文件操作。分别创建 A/B，写入各自待办，在新的独立进程选择 A/B，再修改 A。恢复前后 A 的 Todo ID 一致，B 保持原数据。
  - 替换前注入 `OSError`，旧 JSON 字节保持一致，临时文件清理，返回 `todo_save_failed`。替换后注入 `OSError`，权威文件读回与候选一致，返回 `recovered=true`。保存方法未替换文件却返回时，同样通过读回识别未写入。
  - 替换后注入读回失败，Trace 为 `tool_state_uncertain`，Runtime 停止调用；随后恢复读取可见真实写入。另验证后续 `ModelError` 后新建 SessionStore 仍能读回已确认的待办。
- **T07 交接与限制**。T07 可通过 Runtime 加载的 `SessionState` 读取已有 `messages`，用 SessionStore 保存更新后的四字段快照。本轮初始化并保留历史和记忆字段，对话历史构建及记录留给 T07。支持同一个 Runtime 实例内串行执行，或同目录的 CLI 进程先后运行。同目录并发写入及断电级持久性不在当前保证范围内。
- **最终验收**。独立 Code Review 通过，负责人批准 T06 收尾。全量 `save` 接口修正为 `save(session_id, state, expected=原始快照)` 做乐观并发校验，过期快照不再覆盖较新的 Todo。

## 2026-11-10（T07 多轮历史与基础 Context）

- **Context 与持久化**。`build_messages(history, message)` 校验历史工具调用与结果的顺序及 ID，并拼接同会话历史和本轮用户消息。Runtime 保存本轮新增的用户消息、完整工具批次和真实最终回答。系统指令仅用于模型请求。模型失败或预算触顶仍保存已产生的消息和成对工具结果。
- **追加故障**。Runtime 通过 `append_messages(..., expected_messages=history)` 基于权威 JSON 追加，保留 Todo、Memory 与 Summary。异常后读回磁盘，已写入则视为成功，确认仍是原历史才重试一次。读回失败或发生冲突时停止，已确认 Todo 写入保留。
- **本地验证**。执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，84 项通过，耗时 45.601 秒，退出码 0。新增 8 项。独立 CLI 进程验证了恢复后模型请求携带原 Todo 工具结果。首次完整运行有 3 项旧测试夹具未适配新增历史，调整断言、当前消息计数和读回注入后复跑通过。
- **真实 DeepSeek**。用 `$probe | .\.venv\Scripts\python.exe -B -` 运行脱敏探测，模型为 `deepseek-flash`。首次探测通过普通追问，未通过 Todo 更新断言。复测显式使用 UTF-8，退出码 0。
  - 重新创建 Runtime 并加载同一会话后，模型从前文答出合成睡觉时间 `22:47`。A 和 B 分别保存 4 条和 2 条消息，B 回复未包含 A 的时间。
  - 创建「写论文」的调用 ID 为 `call_00_P9hyNdVacHAbArb6Kfsw0841`。重新加载后，模型对「把刚才那个改到明天」发出更新调用 `call_00_XvpL7I082ujb9qILI9kU3819`。两次 Trace 都证实 handler 启动并成功。权威 JSON 中 Todo ID `8cae9d4c01ae4c1888dcfcae666c0ee5` 保持不变，内容更新为「写论文（明天）」。
  - 两条待办均可能是目标时，模型先询问，工具变更次数为 0，磁盘 Todo 快照保持一致。
- **边界与 T08 交接**。Context 可接受 T08 提供的历史窗口，窗口需保留完整的 assistant/tool 消息组。当前发送完整历史。自然语言指代和歧义判断由模型依据系统指令处理，工具层继续强制可信会话和 ID 校验。
- **最终验收**。独立定向复审通过，负责人批准 T07 收尾。完整测试 87 项通过（退出码 0）。

### 历史协议定向修复

- 负责人确认其他窗口停写，并批准在现有工作树上限定修复 T07。Context 补充了工具结果的字符串 `content` 校验，以及 assistant 工具调用的非空 ID、`type="function"`、函数定义、非空函数名和字符串参数校验。原顺序及调用 ID 配对检查继续生效。非法历史通过既有受控错误路径在调用 Model 前终止。
- 新增 3 项回归测试，覆盖 11 个缺失或格式错误场景。修复前定向运行复现 11 个失败，修复后 3 项全部通过。完整命令为 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，87 项通过，耗时 30.508 秒，退出码 0。
- 已知限制。按负责人决定，Todo 继续自主调用，保留原有澄清提示、可信 Session、Todo ID 和参数校验。目标有歧义时，模型仍可能选择错误但合法的 Todo ID。此轮仅修复历史协议，待独立定向复审。

## 2026-11-10（T08 滚动摘要、长期 Memory 提取与注入）

- **存储约定**。`messages` 是本会话可直接回放的近期窗口。T07 仍先独立追加本轮真实消息，成功压缩后才将早期部分移入摘要。该方案复用四字段 Session JSON 和现有读取入口。每次请求从当前 Session 恢复 `summary`、`memory` 和近期消息。
- **窗口与预算**。已完成的请求在至少剩余两次模型调用预算时检查历史。超过 20 条则从后向前累积完整消息组，保留约 10 条。assistant 工具调用及全部结果作为一组，并沿用 T07 的 ID、顺序和协议校验。Memory 提取、摘要合并各一次，计入每请求 8 次模型调用预算。没有 Session 固定对话轮数上限。
- **Memory 与摘要**。提取模型从旧 Memory 与将移出的短用户原话中返回索引，由 Context 校验并保存所选原文。助手推断、工具输出和摘要不进入提取候选。旧摘要与较早消息合并。模型请求把摘要标为未经核实的历史资料，把 Memory 标为历史用户原话。注入上限为 2,000 字符摘要、16 条各最多 240 字符的同会话 Memory。采用简单的同会话召回，不做语义检索。
- **失败和长度保护**。提取及摘要均成功后，用 `SessionStore.save(..., expected=state)` 一次提交窗口、摘要和 Memory。读取、模型、解析及快照冲突失败时不删除原历史，不重试覆盖。单条用户输入最多 8,000 字符，模型消息采用 64,000 序列化字符预算。历史只能按整组略去，未压缩的原文保留在文件中。压缩源超预算时延后，单次请求最多尝试压缩一次。
- **本地验证**。定向命令 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_compaction.py -v` 通过 12 项，耗时 4.280 秒。全量命令 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` 通过 99 项，耗时 47.055 秒，退出码 0。覆盖边界、工具配对、滚动摘要、跨进程恢复、Todo 追问、会话隔离、长度及故障和冲突保护。
- **真实 DeepSeek**。通过 `$probe | .\.venv\Scripts\python.exe -B -` 运行临时会话探测，使用 `deepseek-flash`，退出码 0。先用真实模型写入合成偏好，再加入脚本生成的 18 条临时消息以触发窗口边界。实际提取与摘要各调用一次，窗口由 22 条压到 10 条，摘要为 240 字符，Memory 保存了用户原话。重建 Runtime 后，模型答出合成暗号 `NORTH-PINE-7421`，该值不在近期消息内。主探测的摘要也包含暗号，另用隔离会话重放同一条 Memory，在摘要为空且近期消息不含暗号时，真实模型仍答出同一值。
- **限制与验收**。Memory 仅选取符合长度的完整用户原话，长期相关性与冲突判断依赖模型。压缩被持续延后时文件可能增长，模型仍受字符预算限制。本轮真实探测为合成召回样例，第 100 轮真实端到端验证留待后续验收。T08 待独立 Code Review。

### T08 独立审查缺陷修复

- **修复前复现**。32 条长历史经短请求后增至 34 条，维护模型调用为 0，Memory 为空。135 条 Todo 的工具结果写入 75,500 字符，后续模型调用受预算阻断。65,000 字符最终回答原样返回和落盘。
- **分批压缩**。Context 按摘要请求与 Memory 提取请求的实际序列化字符数选择最早可处理的完整消息组。每次最多提交一批，两次维护调用仍计入八次预算。单组无法放入预算时保留原文并记录告警，有可处理的后续旧组时仍可推进。Runtime 仍通过权威快照校验一次提交，未处理原文及冲突后的权威历史和 Todo 均保留。
- **有界结果**。超长工具结果在追加前形成最多 8,000 字符的合法 JSON，列表预览保留完整条目及原字符数、总条数和显示条数，保留工具消息顺序、角色和调用 ID。最终回答最多 8,000 字符，附截断标记，返回与保存使用同一文本。
- **修复后原场景复测**。使用确定性模型与真实临时 Session/Todo 文件。两次短请求使持久化历史从 32 条降至 24 条、17 条，两批分别处理 10、9 条，摘要请求为 57,427 和 60,266 字符，后续模型请求注入了早期用户约定。Todo 列表由 75,500 字符缩至 7,892 字符，显示 14/135 条，模型完成两次决策且 135 条 Todo 不变。65,000 字符最终回答返回及落盘均为 8,000 字符，内容一致。
- **测试**。定向命令 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_compaction.py -v` 通过 17 项，新增 5 项，耗时 2.292 秒。全量命令 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` 通过 104 项，耗时 43.414 秒，退出码 0。独立进程读回截断后的工具历史通过 `build_messages()` 配对校验。
- **限制**。无法安全压缩的单组仍保留在文件中，恢复进度依赖后续请求有两次维护预算。超长列表只提供前缀预览，模型无法从该工具消息获得省略条目。
- **最终验收**。独立 Code Review 通过，负责人批准 T08 收尾。定向测试 17 项、全量 104 项通过（退出码 0）。

## 2026-11-10（T09 异常语义、执行限制与完整 Trace）

- **开发基线**。HEAD 为 `540fd62`，工作树干净，T08 的全量基线为 104 项。保留现有 Agent Loop、Session JSON、Todo 提交后读回校验和 Context 压缩算法。
- **模型与工具**。Model 决策分别报告超时、连接错误、API 错误、无效响应或未预期请求错误，错误不带底层请求和响应正文。SDK 沿用 30 秒超时及零重试。Tool Manager 通过可选 `on_start` 在校验通过后、handler 调用前通知 Runtime。未注册工具及 Schema 拒绝均不启动 handler。无状态 handler 抛出的 `TimeoutError` 转为 `tool_timeout`，其他异常沿用结构化工具错误。可恢复工具错误回填后，模型可在剩余预算内修正。
- **持久化与预算**。Todo 写入未能读回核实时记录 `unconfirmed`，同批剩余工具记录 `skipped`，本次请求失败并立即停止模型调用。已核实的写入在后续模型异常或触顶时仍可恢复。第 8 次模型调用返回文本可完成，返回工具请求则保留协议配对，跳过该批执行并返回 `limit_reached`。两次 Context 维护调用计入同一预算；失败按阶段记录，原始历史和已确认的 Todo 保留。
- **Trace**。沿用 `agent_harness.runtime` 的 INFO 日志，`tool_trace` 与 `request_trace` 后接单行 JSON。事件使用 request ID、会话 ID、UTC 时间、序号、决策轮次及工具顺序关联。工具请求、实际启动、结果分别记录。参数仅保留有限字段的类型、字符数及已知 Todo 动作，结果仅记录有限摘要。文本、未知字段名、底层异常详情及完整请求体不进入结构化 Trace。
- **故障注入实测**。定向执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_fault_trace.py -v`，新增 16 项通过，退出码 0。第一次运行的本地引用用例在注册处失败，原因是测试 Schema 缺少要求的根 `type: object`，修正夹具后通过。全量执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，120 项通过，耗时 43.495 秒，退出码 0。
- **脱敏证据**。确定性模型配合实际 Runtime 和临时 Session JSON 的探测退出码为 0。请求 `a626ae91a072405ca013ca6ef64c93f9` 在 UTC 15:47:32 的记录中，`sample-create` 经 requested、started、succeeded，`sample-unknown` 为 rejected 且 `started=false`，第 2 次模型调用为 `model_timeout`。最终状态为 `failed`，重新加载 JSON 仍有 1 条 Todo，历史角色依次为 user、assistant、tool、tool。本轮验证使用故障注入，既有真实 DeepSeek 闭环证据见 T05。
- **交接与限制**。CLI 会话消息输出仍为 `session_id`、`answer`、`status`，业务失败和触顶退出码为 1。日志由调用方配置，默认 CLI 不持久化 Trace。工具超时依赖 handler 主动抛出 `TimeoutError`，同步阻塞的 handler 没有统一的硬截止或取消机制。T09 待独立只读审查。

### T09 独立审查缺陷修复（2026-11-11）

- **修复前复现**。在同一工具批次中使用有常见前缀和无前缀的两种合成密钥。工具均被拒绝，无前缀字符串仍出现在 Trace。独立 CLI 进程执行工具后 stdout 为 JSON、stderr 无 Trace，前置 `--trace` 进入旧的纯文本分支。注入 `os.replace` 成功后抛出 `OSError`，磁盘已经保存摘要和 10 条近期消息，Trace 却报告 `context_maintenance_failed`。
- **Trace 信任边界**。工具名只从本轮已注册定义中取得，未匹配时写入固定标签 `unknown_tool`。Trace 的 `tool_call_id` 由决策轮次和批内位置生成，例如 `call-1-2`，与 `request_id` 一起关联事件。模型返回的原 ID 继续用于协议消息配对及回填。参数与异常仍以有界摘要记录。
- **CLI 查看方式**。运行 `.\.venv\Scripts\python.exe -m agent_harness.cli --trace --session <ID> "<消息>"`。stderr 中每条 Trace 为独立 JSON，stdout 保留原有会话 JSON 和退出码。独立进程实测捕获 5 条事件，依次为 `request_started`、`tool_requested`、`tool_started`、`tool_result`、`request_finished`，退出码为 0。默认模式的同类请求仍无 Trace 输出。
- **Context 保存核查**。比较并保存遇到明确的快照冲突时，直接记录 `failed/session_conflict`。其余保存异常读取权威文件一次。完整快照等于候选时记录 `recovered`，仍等于保存前快照时记录 `failed`，读回失败或观察到第三种状态时记录 `unconfirmed`。候选比对包含消息、Memory、摘要及原 Todo；核查路径只读。
- **故障注入实测**。替换前异常为 `failed`，磁盘保留 21 条消息、空 Memory、空摘要和 1 条 Todo。替换后异常为 `recovered`，磁盘保留 10 条消息、1 条 Memory、摘要和原 Todo。替换后读回失败为 `unconfirmed`，独立重新加载仍读到已压缩状态。另一写入者已提交相同候选时，CAS 拒绝本轮保存并报告冲突；读回同样的 Context 字段但含新增 Todo 时记录 `unconfirmed`，保留权威状态。
- **测试与边界**。定向执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_fault_trace.py -v`，24 项通过，9.713 秒。全量执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，128 项通过，54.892 秒。本轮新增 8 项，两条命令退出码均为 0。验证使用确定性模型和临时会话文件。普通 I/O 异常的读回只能确认快照状态，若两个写入者产生完全相同的快照且没有明确冲突，文件内容无法区分写入来源。T09 待独立定向复审。

### T09 最终限定修复（2026-11-11）

- **复现**。独立 CLI 进程使用 `--trace --session` 请求不存在的会话。Runtime 已依次输出 `request_started`、`session_error`、`request_finished` 三条合法 JSON，CLI 又追加普通错误文本，第四行无法解析。stdout 为空，退出码为 1。
- **修复**。Trace 模式复用 Runtime 已发出的会话失败事件。进入 Runtime 前的受控错误使用单条 `cli_error`，只写固定错误码及失败状态。Trace 命令用法错误同样输出 JSON，保留原用法退出码 2。普通模式继续使用原诊断文本。
- **实测**。新增 4 项独立进程测试。缺失会话只输出上述三条 JSON，stdout 为空，退出码为 1。注入模型超时后，stderr 三条事件均可逐行解析，stdout 保留原 JSON 字段及 `failed` 状态，退出码为 1。配置异常仅输出 `config_error`，异常详情和合成路径未进入输出。普通模式缺失会话仍返回原诊断文本。既有正常工具调用测试继续验证 5 条 JSON Trace、原 stdout 结构及退出码 0。
- **全量回归**。执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，132 项通过，耗时 72.777 秒，退出码 0。
- **最终验收**。独立复审通过，负责人批准 T09 收尾。独立定向测试 28 项（`test_fault_trace.py`）、开发端全量测试 132 项均通过。
- **已知限制**。同步阻塞 handler 无统一硬超时；Trace 默认不持久化，由调用方配置日志。
