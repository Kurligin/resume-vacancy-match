"""Чтение файлов резюме и вакансий в Document с нумерованными строками."""

from rvm.ingest.readers import load_bytes, load_path, load_text

__all__ = ["load_bytes", "load_path", "load_text"]
