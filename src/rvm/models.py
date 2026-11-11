"""Общие типы конвейера. Меняются только по согласованию: на них опираются все модули."""

from enum import StrEnum

from pydantic import BaseModel, Field


class FileStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    BROKEN = "broken"
    ENCRYPTED = "encrypted"
    SCAN_LOW_QUALITY = "scan_low_quality"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    NOT_A_RESUME = "not_a_resume"


class Line(BaseModel):
    """Строка нормализованного текста. n — номер с 1, на него ссылаются цитаты."""

    n: int
    text: str
    block: str | None = (
        None  # блок hh: «Опыт работы», «Ключевые навыки», «Сопроводительное письмо»…
    )
    ocr: bool = False


class Document(BaseModel):
    name: str
    lines: list[Line]
    status: FileStatus = FileStatus.OK
    warnings: list[str] = Field(default_factory=list)

    def numbered(self) -> str:
        """Текст для модели: «[L12] текст»."""
        return "\n".join(f"[L{ln.n}] {ln.text}" for ln in self.lines)


class Section(StrEnum):
    MUST = "must"
    NICE = "nice"
    INFERRED = "inferred"  # выведено из обязанностей
    PROFSTANDARD = "profstandard"
    CONDITION = "condition"


class ReqKind(StrEnum):
    SKILL = "skill"
    TOOL = "tool"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    QUALITY = "quality"
    CONDITION = "condition"
    REQUEST = "request"  # просьба из вакансии («расскажите в письме…»)


class Requirement(BaseModel):
    id: str
    text: str
    section: Section
    kind: ReqKind
    years: float | None = None
    source_line: int | None = None


class Status(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    NO = "no"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    line: int
    quote: str
    source: str = "resume"  # resume | letter


class MatchRow(BaseModel):
    requirement_id: str
    status: Status
    evidence: list[Evidence] = Field(default_factory=list)
    exceeds: str | None = None  # «сверх требования»: почему, цитата — в evidence
    flags: list[str] = Field(default_factory=list)
    note: str = ""
