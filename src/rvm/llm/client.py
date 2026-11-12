"""LLMClient: один вызов модели → проверенный pydantic-объект. Кэш SQLite, demo-режим, учёт токенов."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import openai

# shortcut: приватный модуль openai, но он же строит strict-схему в самом SDK; сломается — скопировать
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError

Messages = list[dict[str, str]]


class LLMError(RuntimeError):
    """Модель дважды вернула ответ, не проходящий схему."""


class DemoCacheMiss(LLMError):
    """demo-режим: ответа нет в кэше, а вызывать модель нельзя."""


class LLMClient:
    def __init__(
        self,
        client: Any = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        cache_path: str | Path | None = None,
    ) -> None:
        env = os.environ
        self.base_url = base_url or env.get("LLM_BASE_URL", "")
        self.model = model or env.get("LLM_MODEL", "")
        self.mode = mode or env.get("LLM_MODE", "live")
        if self.mode not in ("live", "demo"):
            raise ValueError(f"LLM_MODE: ожидается live или demo, получено {self.mode!r}")
        self.cache_path = Path(cache_path or env.get("RVM_CACHE", "var/cache.sqlite"))
        self.usage_path = self.cache_path.parent / "usage.jsonl"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.cache_path)
        self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT)")
        self._client = client
        if client is None and self.mode == "live":
            self._client = openai.OpenAI(
                base_url=self.base_url or None, api_key=api_key or env.get("LLM_API_KEY")
            )
        self._json_object = False  # провайдер отверг json_schema — дальше json_object

    def complete(self, messages: Messages, schema: type[BaseModel], prompt_version: str) -> Any:
        json_schema = to_strict_json_schema(schema)
        key = hashlib.sha256(
            json.dumps(
                [self.model, prompt_version, messages, json_schema],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        row = self._db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            return schema.model_validate_json(row[0])
        if self.mode == "demo":
            raise DemoCacheMiss(f"нет в кэше: {schema.__name__}, {prompt_version}")

        convo = list(messages)
        for attempt in range(2):
            content = self._call(convo, schema, json_schema)
            try:
                result = schema.model_validate_json(content)
            except ValidationError as err:
                if attempt:
                    raise LLMError(f"ответ не прошёл схему {schema.__name__}: {err}") from err
                convo += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"Ответ не прошёл проверку JSON-схемы:\n{err}\n"
                        "Верни исправленный JSON целиком.",
                    },
                ]
                continue
            with self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, result.model_dump_json())
                )
            return result
        raise AssertionError("недостижимо")

    def _call(self, messages: Messages, schema: type[BaseModel], json_schema: dict) -> str:
        kwargs: dict[str, Any] = {"model": self.model, "temperature": 0, "seed": 7}
        if "openrouter" in self.base_url:
            kwargs["extra_body"] = {
                "provider": {"require_parameters": True, "data_collection": "deny"}
            }
        elif "yandex" in self.base_url:
            kwargs["extra_headers"] = {"x-data-logging-enabled": "false"}
        if not self._json_object:
            try:
                fmt = {"name": schema.__name__, "schema": json_schema, "strict": True}
                return self._send(messages, {"type": "json_schema", "json_schema": fmt}, kwargs)
            except openai.BadRequestError:
                self._json_object = True
        hint = f"Ответь только JSON по схеме:\n{json.dumps(json_schema, ensure_ascii=False)}"
        if messages and messages[0]["role"] == "system":
            first = {"role": "system", "content": f"{messages[0]['content']}\n\n{hint}"}
            messages = [first, *messages[1:]]
        else:
            messages = [{"role": "system", "content": hint}, *messages]
        return self._send(messages, {"type": "json_object"}, kwargs)

    def _send(self, messages: Messages, response_format: dict, kwargs: dict[str, Any]) -> str:
        resp = self._client.chat.completions.create(
            messages=messages, response_format=response_format, **kwargs
        )
        if resp.usage is not None:
            with self.usage_path.open("a", encoding="utf-8") as f:
                rec = {
                    "model": self.model,
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                }
                f.write(json.dumps(rec) + "\n")
        return resp.choices[0].message.content or ""
