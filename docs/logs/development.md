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
