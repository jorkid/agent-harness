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
