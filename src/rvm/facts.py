"""Факты резюме: места работы, образование, условия. Даты и стаж считает код (rvm.experience)."""

from datetime import date

from pydantic import BaseModel, Field


class Period(BaseModel):
    """Место работы. Строки line_from..line_to — весь блок места: даты, должность, описание."""

    org: str | None = None
    title: str | None = None
    line_from: int
    line_to: int
    dates_raw: str  # даты как в тексте: «март 2023 — настоящее время»
    start: date | None = None  # первое число месяца; None — даты не разобраны
    end: date | None = None  # None при current
    current: bool = False  # «по настоящее время»
    declared_raw: str | None = None  # заявленная длительность как в тексте: «(2 года)»
    declared_months: int | None = None


class Education(BaseModel):
    level: str | None  # «высшее», «бакалавр», «курсы»…
    field: str | None  # направление
    institution: str | None
    year: int | None
    finished: bool | None


class ResumeFacts(BaseModel):
    periods: list[Period] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    city: str | None = None
    work_formats: list[str] = Field(default_factory=list)
    declared_total_raw: str | None = None  # «Опыт работы — 7 лет 9 месяцев»
    declared_total_months: int | None = None
    letter_lines: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Discrepancy(BaseModel):
    """Заявленный стаж расходится с посчитанным по датам."""

    period: int | None  # индекс в ResumeFacts.periods; None — общий стаж
    declared: int
    computed: int
