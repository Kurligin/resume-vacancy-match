"""LLMClient: один вызов модели → проверенный pydantic-объект.

Кэш SQLite со сроком жизни, demo-режим, учёт токенов (только счётчики, без текста).
"""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import openai

# shortcut: приватный модуль openai, но он же строит strict-схему в самом SDK; сломается — скопировать
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError

Messages = list[dict[str, str]]


class LLMError(RuntimeError):
    """Модель дважды вернула ответ, не проходящий схему."""


class LLMTruncated(LLMError):
    """Ответ обрезан по лимиту длины: JSON неполный, повтор с тем же лимитом не поможет."""


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
        self.max_tokens = int(env.get("LLM_MAX_TOKENS", "4096"))
        if self.mode not in ("live", "demo"):
            raise ValueError(f"LLM_MODE: ожидается live или demo, получено {self.mode!r}")
        self.cache_path = Path(cache_path or env.get("RVM_CACHE", "var/cache.sqlite"))
        self.usage_path = self.cache_path.parent / "usage.jsonl"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # в кэше ответы модели по резюме (ПДн) — храним не дольше срока
        self.ttl = float(env.get("RVM_CACHE_TTL_DAYS", "30")) * 86400
        self._sql(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, created REAL)"
        )
        try:
            self._sql("ALTER TABLE cache ADD COLUMN created REAL")  # кэш старой схемы
        except sqlite3.OperationalError:
            pass
        self._client = client
        if client is None and self.mode == "live":
            self._client = openai.OpenAI(
                base_url=self.base_url or None, api_key=api_key or env.get("LLM_API_KEY")
            )
        self._json_object = False  # провайдер отверг json_schema — дальше json_object
        self._reasoning_off = "openrouter" in self.base_url  # модель без рассуждений — сброс

    def complete(self, messages: Messages, schema: type[BaseModel], prompt_version: str) -> Any:
        json_schema = to_strict_json_schema(schema)
        key = hashlib.sha256(
            json.dumps(
                [self.model, prompt_version, messages, json_schema],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        rows = self._sql(
            "SELECT value FROM cache WHERE key = ? AND created >= ?", (key, time.time() - self.ttl)
        )
        if rows:
            return schema.model_validate_json(rows[0][0])
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
            self._sql(
                "INSERT OR REPLACE INTO cache (key, value, created) VALUES (?, ?, ?)",
                (key, result.model_dump_json(), time.time()),
            )
            return result
        raise AssertionError("недостижимо")

    def purge_expired(self) -> int:
        """Удалить записи кэша старше срока жизни; вернуть их число."""
        cutoff = time.time() - self.ttl
        with closing(sqlite3.connect(self.cache_path, timeout=30)) as db, db:
            cur = db.execute("DELETE FROM cache WHERE created IS NULL OR created < ?", (cutoff,))
            return cur.rowcount

    def _sql(self, query: str, args: tuple = ()) -> list[Any]:
        # соединение на вызов: клиент работает из пула потоков веб-сервера
        with closing(sqlite3.connect(self.cache_path, timeout=30)) as db, db:
            return db.execute(query, args).fetchall()

    def _call(self, messages: Messages, schema: type[BaseModel], json_schema: dict) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "seed": 7,
            "max_tokens": self.max_tokens,
        }
        if "openrouter" in self.base_url:
            provider: dict[str, Any] = {"require_parameters": True, "data_collection": "deny"}
            # одну модель держат несколько хостеров, скорость у них различается в разы
            if sort := os.environ.get("LLM_PROVIDER_SORT", "throughput"):
                provider["sort"] = sort
            kwargs["extra_body"] = {"provider": provider}
            effort = os.environ.get("LLM_REASONING_EFFORT") or (
                "low" if "gpt-oss" in self.model else ""
            )
            if effort:
                # модели, которые рассуждают всегда (gpt-oss): только минимум рассуждений
                kwargs["extra_body"]["reasoning"] = {"effort": effort}
            elif self._reasoning_off:
                # гибридные модели (qwen3) иначе «думают» в десятки раз дольше ответа
                kwargs["extra_body"]["reasoning"] = {"enabled": False}
        elif "yandex" in self.base_url:
            kwargs["extra_headers"] = {"x-data-logging-enabled": "false"}
        if not self._json_object:
            try:
                fmt = {"name": schema.__name__, "schema": json_schema, "strict": True}
                return self._send(messages, {"type": "json_schema", "json_schema": fmt}, kwargs)
            except openai.NotFoundError:
                if not self._reasoning_off:
                    raise
                # с require_parameters модель без рассуждений не находит провайдера
                self._reasoning_off = False
                return self._call(messages, schema, json_schema)
            except openai.BadRequestError:
                if self._reasoning_off and "reasoning" in kwargs.get("extra_body", {}):
                    # параметр рассуждений не принят — повторяем без него, схему не теряем
                    self._reasoning_off = False
                    return self._call(messages, schema, json_schema)
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
        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMTruncated(
                f"ответ модели обрезан по лимиту {self.max_tokens} токенов — "
                "увеличьте LLM_MAX_TOKENS или сократите текст"
            )
        return choice.message.content or ""
