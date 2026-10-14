from paperless_mineru.config import MineruSettings, mineru_lang_from_tesseract


def test_disabled_without_endpoint():
    s = MineruSettings.from_env()
    assert not s.enabled
    assert s.api == "auto"
    assert s.score == 20
    assert s.lang == "ch"
    assert s.ocr_mode == "auto"
    assert s.content_format == "markdown"
    assert s.text_layer and s.pdfa


def test_reads_env(monkeypatch):
    monkeypatch.setenv("PAPERLESS_MINERU_ENDPOINT", "http://mineru:8000/ ")
    monkeypatch.setenv("PAPERLESS_MINERU_API", "legacy")
    monkeypatch.setenv("PAPERLESS_MINERU_BACKEND", "pipeline")
    monkeypatch.setenv("PAPERLESS_MINERU_EFFORT", "high")
    monkeypatch.setenv("PAPERLESS_MINERU_TIMEOUT", "60")
    monkeypatch.setenv("PAPERLESS_MINERU_TABLE_ENABLE", "false")
    monkeypatch.setenv("PAPERLESS_MINERU_SCORE", "30")
    s = MineruSettings.from_env()
    assert s.enabled
    assert s.endpoint == "http://mineru:8000"
    assert (s.api, s.backend, s.effort, s.timeout, s.table_enable, s.score) == (
        "legacy",
        "pipeline",
        "high",
        60.0,
        False,
        30,
    )


def test_invalid_values_fall_back(monkeypatch):
    monkeypatch.setenv("PAPERLESS_MINERU_API", "v9")
    monkeypatch.setenv("PAPERLESS_MINERU_OCR_MODE", "magic")
    monkeypatch.setenv("PAPERLESS_MINERU_TIMEOUT", "soon")
    monkeypatch.setenv("PAPERLESS_MINERU_TEXT_LAYER", "maybe")
    s = MineruSettings.from_env()
    assert (s.api, s.ocr_mode, s.timeout, s.text_layer) == ("auto", "auto", 900.0, True)


def test_language_mapping(monkeypatch):
    assert mineru_lang_from_tesseract("deu") == "ch"
    assert mineru_lang_from_tesseract("deu+rus") == "east_slavic"
    assert mineru_lang_from_tesseract(None) == "ch"
    monkeypatch.setenv("PAPERLESS_OCR_LANGUAGE", "ell")
    assert MineruSettings.from_env().lang == "el"
    monkeypatch.setenv("PAPERLESS_MINERU_LANG", "korean")
    assert MineruSettings.from_env().lang == "korean"
