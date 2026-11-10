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
