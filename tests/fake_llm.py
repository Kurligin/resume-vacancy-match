"""Фейковый транспорт OpenAI-совместимого API: отдаёт заготовленные ответы, пишет вызовы."""

from types import SimpleNamespace
from typing import Any


class FakeOpenAI:
    def __init__(self, replies: list[str | Exception], finish_reason: str = "stop") -> None:
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=reply), finish_reason=self.finish_reason
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
