"""Чтение файлов резюме и вакансий в Document с нумерованными строками."""

from rvm.ingest.readers import load_bytes, load_path, load_text

# расширения файлов резюме (формат load_bytes определяет по содержимому; список — для отбора
# вложений и файлов, как accept в форме загрузки)
SUFFIXES = (
    ".docx", ".doc", ".pdf", ".rtf", ".txt", ".odt", ".html", ".htm",
    ".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff",
)  # fmt: skip

__all__ = ["SUFFIXES", "load_bytes", "load_path", "load_text"]
