"""Фейковые транспорты модели: тесты и показ интерфейса без ключа. Сеть не используется."""

import json
import re
from types import SimpleNamespace
from typing import Any

_LINE = re.compile(r"^\[L(\d+)\] (.+)$", re.MULTILINE)
_REQ = re.compile(r"^(R\d+): (.+) \(\w+", re.MULTILINE)
_WORD = re.compile(r"[^\W_]{3,}")
# общие слова требований: по ним совпадение ничего не значит
_STOP = {"опыт", "работ", "знани", "умени", "навык", "уверен", "хорош", "владе", "требо", "плюсо"}
MAX_REQS = 8


class RoutingFake:
    """Транспорт модели: ответ по имени схемы; пишет все сообщения; fail — ломает сопоставление
    для вакансии, в требованиях которой есть эта подстрока."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        name = kwargs["response_format"]["json_schema"]["name"]
        user = kwargs["messages"][-1]["content"]
        if name == "_Items":
            reply = {
                "items": [
                    {
                        "text": f"требование {i} {user.splitlines()[0][:30]}",
                        "section": "must",
                        "kind": "skill",
                        "years": None,
                        "source_line": i,
                    }
                    for i in (1, 2)
                ]
            }
        elif name == "_Markup":
            reply = {
                "periods": [],
                "education": [],
                "city": None,
                "work_formats": [],
                "declared_total_raw": None,
                "letter_lines": [],
            }
        else:
            if self.fail and any(self.fail in m["content"] for m in kwargs["messages"]):
                return self._reply("не json")
            ids = re.findall(r"^(R\d+):", user, re.MULTILINE)
            reply = {
                "items": [
                    {
                        "requirement_id": i,
                        "status": "unknown",
                        "evidence": [],
                        "exceeds": None,
                        "note": "",
                        "self_claim": False,
                        "skill": None,
                    }
                    for i in ids
                ]
            }
        return self._reply(json.dumps(reply, ensure_ascii=False))

    @staticmethod
    def _reply(content: str) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    def sent(self) -> str:
        return "\n".join(m["content"] for c in self.calls for m in c["messages"])


class DemoFake(RoutingFake):
    """Показ без ключа: требования — строки вакансии после «Требования», статус — по
    совпадению основ слов со строкой резюме (2+ основы — полное, 1 — частично)."""

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        name = kwargs["response_format"]["json_schema"]["name"]
        user = kwargs["messages"][-1]["content"]
        if name == "_Items":
            reply = {"items": self._requirements(user)}
        elif name == "_Answers":
            reply = {"items": self._answers(user)}
        else:
            return super()._create(**kwargs)
        self.calls.append(kwargs)
        return self._reply(json.dumps(reply, ensure_ascii=False))

    @staticmethod
    def _stems(text: str) -> set[str]:
        return {w[:5] for w in _WORD.findall(text.lower()) if w[:5] not in _STOP}

    def _requirements(self, user: str) -> list[dict[str, Any]]:
        lines = [(int(n), t.strip()) for n, t in _LINE.findall(user)][1:]  # без названия
        head = next((i for i, (_, t) in enumerate(lines) if "требован" in t.lower()), -1)
        picked = [(n, t) for n, t in lines[head + 1 :] if 2 <= len(t.split()) <= 14]
        picked = [(n, t) for n, t in picked if not t.endswith(":")][:MAX_REQS]
        return [
            {
                "text": t,
                "section": "must" if i < len(picked) / 2 else "nice",
                "kind": "skill",
                "years": None,
                "source_line": n,
            }
            for i, (n, t) in enumerate(picked)
        ]

    def _answers(self, user: str) -> list[dict[str, Any]]:
        resume, _, reqs = user.partition("\n\nТребования:\n")
        lines = [(int(n), t) for n, t in _LINE.findall(resume)]
        items = []
        for rid, text in _REQ.findall(reqs):
            stems = self._stems(text)
            hits, n, line = max(
                ((sum(s in t.lower() for s in stems), n, t) for n, t in lines),
                default=(0, 0, ""),
            )
            status = "full" if hits >= 2 else "partial" if hits == 1 else "unknown"
            items.append(
                {
                    "requirement_id": rid,
                    "status": status,
                    "evidence": [{"line": n, "quote": line[:120]}] if hits else [],
                    "exceeds": None,
                    "note": "",
                    "self_claim": False,
                    "skill": None,
                }
            )
        return items
