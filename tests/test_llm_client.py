import json
from pathlib import Path

import openai
import pytest
from fake_llm import FakeOpenAI
from pydantic import BaseModel

from rvm.llm import DemoCacheMiss, LLMClient, LLMError, LLMTruncated


class Answer(BaseModel):
    value: int


MSGS = [{"role": "user", "content": "сколько?"}]


def make(tmp_path: Path, fake: FakeOpenAI | None, **kw: str) -> LLMClient:
    kw.setdefault("base_url", "http://local/v1")
    kw.setdefault("mode", "live")
    return LLMClient(fake, model="m", cache_path=tmp_path / "cache.sqlite", **kw)


def test_parses_and_sends_strict_schema(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": 3}'])
    assert make(tmp_path, fake).complete(MSGS, Answer, "v1") == Answer(value=3)
    call = fake.calls[0]
    assert call["temperature"] == 0 and call["seed"] == 7
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    usage = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
    assert usage == {"model": "m", "prompt_tokens": 10, "completion_tokens": 5}


def test_cache_hit_skips_model(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": 3}'])
    make(tmp_path, fake).complete(MSGS, Answer, "v1")
    again = make(tmp_path, FakeOpenAI([])).complete(MSGS, Answer, "v1")
    assert again == Answer(value=3) and len(fake.calls) == 1


def test_prompt_version_changes_key(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": 3}', '{"value": 4}'])
    client = make(tmp_path, fake)
    client.complete(MSGS, Answer, "v1")
    assert client.complete(MSGS, Answer, "v2") == Answer(value=4)


def test_retry_with_error_then_ok(tmp_path: Path) -> None:
    fake = FakeOpenAI(["не json", '{"value": 1}'])
    assert make(tmp_path, fake).complete(MSGS, Answer, "v1") == Answer(value=1)
    retry_msgs = fake.calls[1]["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": "не json"}
    assert "Answer" in retry_msgs[-1]["content"] or "JSON" in retry_msgs[-1]["content"]


def test_second_invalid_raises(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": "x"}', '{"other": 1}'])
    with pytest.raises(LLMError):
        make(tmp_path, fake).complete(MSGS, Answer, "v1")


def test_demo_miss(tmp_path: Path) -> None:
    with pytest.raises(DemoCacheMiss):
        make(tmp_path, None, mode="demo").complete(MSGS, Answer, "v1")


def test_demo_hit(tmp_path: Path) -> None:
    make(tmp_path, FakeOpenAI(['{"value": 2}'])).complete(MSGS, Answer, "v1")
    assert make(tmp_path, None, mode="demo").complete(MSGS, Answer, "v1") == Answer(value=2)


def test_schema_rejected_falls_back_to_json_object(tmp_path: Path) -> None:
    # без HTTP-ответа: конструктор ошибки завязан на транспорт SDK, клиенту нужен только тип
    err = openai.BadRequestError.__new__(openai.BadRequestError)
    Exception.__init__(err, "response_format json_schema unsupported")
    fake = FakeOpenAI([err, '{"value": 5}'])
    assert make(tmp_path, fake).complete(MSGS, Answer, "v1") == Answer(value=5)
    second = fake.calls[1]
    assert second["response_format"] == {"type": "json_object"}
    assert '"value"' in second["messages"][0]["content"]


def test_openrouter_extra_body(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": 1}'])
    make(tmp_path, fake, base_url="https://openrouter.ai/api/v1").complete(MSGS, Answer, "v1")
    body = fake.calls[0]["extra_body"]
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert body["reasoning"] == {"enabled": False}  # рассуждения съедали лимит и время
    assert fake.calls[0]["max_tokens"] == 4096


def test_max_tokens_from_env_and_no_reasoning_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MAX_TOKENS", "1000")
    fake = FakeOpenAI(['{"value": 1}'])
    make(tmp_path, fake).complete(MSGS, Answer, "v1")
    assert fake.calls[0]["max_tokens"] == 1000
    assert "extra_body" not in fake.calls[0]


def test_truncated_answer_is_clear_error(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": '], finish_reason="length")
    with pytest.raises(LLMTruncated, match="LLM_MAX_TOKENS"):
        make(tmp_path, fake).complete(MSGS, Answer, "v1")
    assert len(fake.calls) == 1  # без повтора: тот же лимит обрежет снова


def test_yandex_header(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": 1}'])
    make(tmp_path, fake, base_url="https://llm.api.cloud.yandex.net/v1").complete(
        MSGS, Answer, "v1"
    )
    assert fake.calls[0]["extra_headers"] == {"x-data-logging-enabled": "false"}


def test_cache_from_many_threads(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    llm = make(tmp_path, FakeOpenAI([f'{{"value": {i}}}' for i in range(8)]))

    def ask(i: int) -> Answer:
        return llm.complete([{"role": "user", "content": str(i)}], Answer, "v1")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(ask, range(8)))
    assert sorted(r.value for r in results) == list(range(8))
    assert [ask(i) for i in range(8)] == results  # теперь из кэша, фейк пуст


def test_cache_ttl_and_purge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    import time

    monkeypatch.setenv("RVM_CACHE_TTL_DAYS", "30")
    llm = make(tmp_path, FakeOpenAI(['{"value": 1}', '{"value": 2}']))
    assert llm.complete(MSGS, Answer, "v1").value == 1
    old = time.time() - 31 * 86400
    with sqlite3.connect(tmp_path / "cache.sqlite") as db:
        db.execute("UPDATE cache SET created = ?", (old,))
    assert llm.complete(MSGS, Answer, "v1").value == 2  # старая запись не отдаётся
    with sqlite3.connect(tmp_path / "cache.sqlite") as db:
        db.execute("UPDATE cache SET created = ?", (old,))
    assert llm.purge_expired() == 1
    with sqlite3.connect(tmp_path / "cache.sqlite") as db:
        assert db.execute("SELECT count(*) FROM cache").fetchone()[0] == 0


def test_openrouter_model_without_reasoning_retries_without_it(tmp_path: Path) -> None:
    # require_parameters: модель без рассуждений не найдёт провайдера с параметром reasoning
    err = openai.NotFoundError.__new__(openai.NotFoundError)
    Exception.__init__(err, "No endpoints found that can handle the requested parameters")
    fake = FakeOpenAI([err, '{"value": 1}', '{"value": 2}'])
    llm = make(tmp_path, fake, base_url="https://openrouter.ai/api/v1")
    assert llm.complete(MSGS, Answer, "v1") == Answer(value=1)
    assert llm.complete(MSGS, Answer, "v2") == Answer(value=2)
    assert "reasoning" in fake.calls[0]["extra_body"]
    assert all("reasoning" not in c["extra_body"] for c in fake.calls[1:])
    assert fake.calls[1]["response_format"]["type"] == "json_schema"
