"""DeepSeek 模型客户端：只负责发起一次纯文本 Chat Completion 并返回文本。

职责：
- 使用 OpenAI 兼容协议连接 DeepSeek。
- 把网络/API 异常转换为不含密钥的 ModelError。
- 本模块不做工具调用、不做 Agent Loop。
"""

from __future__ import annotations

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
)

from agent_harness.config import Settings


class ModelError(Exception):
    """模型调用失败。信息已脱敏，不包含 API Key 或原始敏感请求。"""


class Model:
    """DeepSeek 文本模型的最小封装。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    def chat(self, text: str) -> str:
        """发送一条用户文本，返回模型回复文本；失败时抛出 ModelError。"""
        try:
            response = self._client.chat.completions.create(
                model=self._settings.model,
                messages=[{"role": "user", "content": text}],
            )
        except AuthenticationError as exc:
            raise ModelError(
                "API Key 无效或无权限，请检查 DEEPSEEK_API_KEY。"
            ) from exc
        except APITimeoutError as exc:
            raise ModelError("请求超时。") from exc
        except APIConnectionError as exc:
            raise ModelError("无法连接模型服务，请检查网络或 BASE_URL。") from exc
        except APIError as exc:
            raise ModelError(f"模型服务返回错误：{type(exc).__name__}") from exc
        except Exception as exc:  # 兜底，避免主流程因未预期异常崩溃
            raise ModelError(f"调用模型时发生未预期错误：{type(exc).__name__}") from exc

        if not response.choices:
            raise ModelError("模型返回了空结果。")
        content = response.choices[0].message.content
        if not content:
            raise ModelError("模型返回了空内容。")
        return content
