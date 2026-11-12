import json
from pathlib import Path

import openai
import pytest
from fake_llm import FakeOpenAI
from pydantic import BaseModel

from rvm.llm import DemoCacheMiss, LLMClient, LLMError


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
    provider = fake.calls[0]["extra_body"]["provider"]
    assert provider == {"require_parameters": True, "data_collection": "deny"}


def test_yandex_header(tmp_path: Path) -> None:
    fake = FakeOpenAI(['{"value": 1}'])
    make(tmp_path, fake, base_url="https://llm.api.cloud.yandex.net/v1").complete(
        MSGS, Answer, "v1"
    )
    assert fake.calls[0]["extra_headers"] == {"x-data-logging-enabled": "false"}
