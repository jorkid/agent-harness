"""配置读取：从环境变量或 .env 文件加载 DeepSeek 相关配置。

职责：
- 只负责读取与校验配置，不包含任何模型调用逻辑。
- API Key 只从环境变量或本地 .env 读取，不硬编码、不打印。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 默认值（可通过环境变量 / .env 覆盖）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 环境变量名
ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_MODEL = "DEEPSEEK_MODEL"


class ConfigError(Exception):
    """配置错误（例如缺少 API Key）。信息中不包含密钥本身。"""


@dataclass(frozen=True)
class Settings:
    """一次运行所需的配置集合。"""

    api_key: str
    base_url: str
    model: str


def load_settings(env_file: Path | None = None) -> Settings:
    """加载配置。

    - 先加载项目根目录的 .env（不覆盖已存在的真实环境变量）。
    - API Key 必填，缺失时抛出 ConfigError，错误信息只提及变量名，不含密钥。
    """
    if env_file is None:
        env_file = Path.cwd() / ".env"
    # override=False：真实环境变量优先于 .env 文件中的值
    load_dotenv(dotenv_path=env_file, override=False)

    api_key = os.getenv(ENV_API_KEY)
    if not api_key or not api_key.strip():
        raise ConfigError(
            f"缺少 {ENV_API_KEY}。请复制 .env.example 为 .env 并填入密钥，"
            f"或设置环境变量 {ENV_API_KEY}。"
        )

    base_url = os.getenv(ENV_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    model = os.getenv(ENV_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL

    return Settings(
        api_key=api_key.strip(),
        base_url=base_url,
        model=model,
    )
