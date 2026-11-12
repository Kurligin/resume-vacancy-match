"""Лимиты чтения: один файл не должен класть сервис или раздувать запрос к модели."""

MAX_BYTES = 15 * 1024 * 1024  # размер загруженного файла

# docx и прочие zip: сумма распакованных размеров и коэффициент сжатия
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
MAX_ZIP_RATIO = 100

# текст документа после нормализации
MAX_LINES = 2000
MAX_CHARS = 60_000

# текстовый PDF
MAX_PDF_PAGES = 20
PDF_SECONDS = 30.0

# распознавание сканов
MAX_OCR_PAGES = 10
MAX_PIXELS = 40_000_000  # на страницу или картинку
OCR_PAGE_SECONDS = 30.0  # timeout одного вызова Tesseract
OCR_TOTAL_SECONDS = 120.0
