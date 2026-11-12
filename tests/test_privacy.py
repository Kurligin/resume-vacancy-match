import os
import re
from pathlib import Path

import pytest

from rvm.models import Document, Line
from rvm.privacy import PrivacyError, assert_clean, contact_hash, mask, swap_identity

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")


def doc_of(*texts: str, name: str = "резюме.txt") -> Document:
    return Document(name=name, lines=[Line(n=i + 1, text=t) for i, t in enumerate(texts)])


def text_of(doc: Document) -> str:
    return "\n".join(line.text for line in doc.lines)


@pytest.mark.parametrize(
    "phone",
    ["+7 (916) 123-45-67", "8 916 123 45 67", "89161234567", "+79161234567", "8(916)1234567"],
)
def test_phones_masked(phone: str) -> None:
    m = mask(doc_of(f"Телефон: {phone}, звонить вечером"))
    assert m.doc.lines[0].text == "Телефон: [телефон], звонить вечером"


def test_contacts_links_nicks() -> None:
    m = mask(
        doc_of(
            "Почта: a.ivanov+job@mail.ru",
            "Telegram: @alex_dev, https://t.me/alex_dev",
            "github.com/alexdev и hh.ru/resume/abc123",
        )
    )
    text = text_of(m.doc)
    for leak in ("mail.ru", "alex_dev", "alexdev", "abc123"):
        assert leak not in text
    assert "[почта]" in text and "[ссылка]" in text
    assert m.found["почта"] == 1


def test_birth_age_sex_address_but_city_and_experience_stay() -> None:
    m = mask(
        doc_of(
            "Женщина, 31 год, родилась 12 марта 1995",
            "Дата рождения: 01.02.1990",
            "Возраст: 40 лет",
            "Пол: мужской",
            "г. Москва, ул. Ленина, д. 5, кв. 12",
            "Опыт работы более 7 лет, 14 лет 3 месяца в бухгалтерии",
            "Полная занятость",
        )
    )
    lines = [line.text for line in m.doc.lines]
    assert lines[0] == "[пол], [возраст], [дата рождения]"
    assert lines[1] == "[дата рождения]"
    assert lines[2] == "[возраст]"
    assert lines[3] == "[пол]"
    assert lines[4].startswith("г. Москва") and "Ленина" not in lines[4] and "12" not in lines[4]
    assert lines[5] == "Опыт работы более 7 лет, 14 лет 3 месяца в бухгалтерии"
    assert lines[6] == "Полная занятость"


def test_names_by_ner_and_by_filename_in_cases() -> None:
    m = mask(
        doc_of(
            "Меня зовут Мария Кузнецова.",
            "Резюме Петровой Лилии, бухгалтер",
            "Работал с Docker, Vue и Nuxt.",
            name="Петрова Лилия_резюме 4.docx",
        )
    )
    lines = [line.text for line in m.doc.lines]
    assert "Мария" not in lines[0] and "Кузнецова" not in lines[0] and "[ФИО]" in lines[0]
    assert lines[1] == "Резюме [ФИО] [ФИО], бухгалтер"
    assert lines[2] == "Работал с Docker, Vue и Nuxt."


def test_line_numbers_kept() -> None:
    src = doc_of("Иван Петров", "", "+7 916 123 45 67", "Vue 3")
    m = mask(src)
    assert [line.n for line in m.doc.lines] == [line.n for line in src.lines]
    assert m.doc.lines[1].text == "" and m.doc.lines[3].text == "Vue 3"


def test_assert_clean_catches_planted_phone() -> None:
    m = mask(doc_of("Иван Петров", "Опыт 5 лет"))
    assert_clean(m)
    m.doc.lines[1].text += ", звоните 8 (916) 123-45-67"
    with pytest.raises(PrivacyError):
        assert_clean(m)


def test_contact_hash_normalized() -> None:
    assert contact_hash("+7 (916) 123-45-67") == contact_hash("89161234567")
    assert contact_hash("A.Ivanov@Mail.RU ") == contact_hash("a.ivanov@mail.ru")
    m1 = mask(doc_of("+7 (916) 123-45-67"))
    m2 = mask(doc_of("тел. 89161234567"))
    assert m1.contact_hashes == m2.contact_hashes == [contact_hash("89161234567")]


def test_contact_hash_salted(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = contact_hash("89161234567")
    monkeypatch.setenv("RVM_HASH_SALT", "соль")
    assert contact_hash("89161234567") != plain


def test_swap_identity() -> None:
    src = doc_of("Иванов", "Алексей", "Мужчина, 25 лет", name="Иванов Алексей_резюме 1.docx")
    out = swap_identity(src, "Петрова Мария", "ж")
    assert [line.text for line in out.lines] == ["Петрова", "Мария", "Женщина, 25 лет"]
    assert "Иванов" not in out.name
    assert src.lines[0].text == "Иванов"


# --- файлы кейса ---


def case_resumes() -> list[Path]:
    return sorted(Path(DATA_DIR or ".").glob("*резюме*"))


@needs_data
def test_case_resumes_masked() -> None:
    from rvm.ingest import load_path

    files = case_resumes()
    assert len(files) == 4
    texts = {}
    for path in files:
        src = load_path(path)
        m = mask(src, path.name)
        assert [line.n for line in m.doc.lines] == [line.n for line in src.lines]
        text = text_of(m.doc)
        for word in re.findall(r"[А-ЯЁ][а-яё]+", path.stem):
            if word != "Резюме":
                assert not re.search(rf"\b{word}\b", text), (path.name, word)
        assert not re.search(r"Мужчина|Женщина", text)
        assert not re.search(r"\[пол\],?\s*\d+\s*(год|лет)", text)
        assert not re.search(r"@|\+7|\d{3}[- ]\d{2}[- ]\d{2}", text)
        assert_clean(m)
        texts[path.name] = text
        # инвариант: подмена ФИО и пола не меняет то, что видит модель
        swapped = swap_identity(src, "Кузнецова Мария", "ж")
        assert text_of(mask(swapped).doc) == text
    assert "г. Москва" in texts["Иванов Алексей_резюме 1.docx"]
    assert "Долгопрудный" in texts["Петрова Лилия_резюме 4.docx"]


def test_year_with_g_is_not_birth_date() -> None:
    m = mask(doc_of("В 2020 г. работал в МФТИ", "1990 г.р."))
    assert m.doc.lines[0].text == "В 2020 г. работал в МФТИ"
    assert m.doc.lines[1].text == "[дата рождения]"


def test_swap_identity_filename_overlapping_names() -> None:
    src = doc_of("Петров Иван", name="Петров Иван_резюме 2.docx")
    out = swap_identity(src, "Иванова Мария", "ж")
    assert out.name == "Иванова Мария_резюме 2.docx"
    assert out.lines[0].text == "Иванова Мария"
