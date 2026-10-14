"""Parser behaviour with Paperless modules stubbed and MinerU mocked."""

from __future__ import annotations

import json

import pytest
from conftest import FakeOcrConfig, FakeParseError
from pdfminer.high_level import extract_text

from paperless_mineru import parser as parser_mod
from paperless_mineru.client import MineruError, MineruResult
from paperless_mineru.parser import MineruDocumentParser

_REQUIRED_ATTRS = ("name", "version", "author", "url", "supported_mime_types", "score")


class FakeClient:
    """Replaces MineruClient; records the uploaded file."""

    result: MineruResult | Exception | None = None
    uploads: list = []

    def __init__(self, settings):
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def parse(self, path, *, filename=None):
        FakeClient.uploads.append((path, filename))
        if isinstance(FakeClient.result, Exception):
            raise FakeClient.result
        return FakeClient.result


@pytest.fixture
def fake_client(monkeypatch, legacy_result):
    doc = legacy_result["results"]["scan"]
    FakeClient.result = MineruResult(
        markdown=doc["md_content"] + "\n\n![](images/x.jpg)",
        middle_json=json.loads(doc["middle_json"]),
        api="legacy",
        server_version="3.4.0",
    )
    FakeClient.uploads = []
    monkeypatch.setattr(parser_mod, "MineruClient", FakeClient)
    monkeypatch.setenv("PAPERLESS_MINERU_ENDPOINT", "http://mineru.test:8000")
    return FakeClient


def test_registry_contract():
    for attr in _REQUIRED_ATTRS:
        assert hasattr(MineruDocumentParser, attr)
    assert MineruDocumentParser.uses_remote_service is True
    assert MineruDocumentParser.supported_mime_types()["application/pdf"] == ".pdf"
    for method in (
        "configure",
        "parse",
        "get_text",
        "get_date",
        "get_archive_path",
        "get_thumbnail",
        "get_page_count",
        "extract_metadata",
        "__enter__",
        "__exit__",
    ):
        assert callable(getattr(MineruDocumentParser, method))


def test_score(monkeypatch):
    assert MineruDocumentParser.score("application/pdf", "a.pdf") is None  # not configured
    monkeypatch.setenv("PAPERLESS_MINERU_ENDPOINT", "http://mineru.test:8000")
    assert MineruDocumentParser.score("application/pdf", "a.pdf") == 20
    assert MineruDocumentParser.score("image/png", "a.png") == 20
    assert MineruDocumentParser.score("text/plain", "a.txt") is None
    monkeypatch.setenv("PAPERLESS_MINERU_SCORE", "5")
    assert MineruDocumentParser.score("application/pdf", "a.pdf") == 5


def test_parse_scanned_pdf_produces_searchable_archive(paperless_stubs, fake_client, invoice):
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        assert p.can_produce_archive and not p.requires_pdf_rendition
        p.parse(scan_pdf, "application/pdf", produce_archive=True)
        text = p.get_text()
        assert text.startswith("## Rechnung Nr. 2026-0815")
        assert "images/" not in text
        archive = p.get_archive_path()
        assert archive is not None and archive.exists()
        assert "Gesamtbetrag: 1.234,56 EUR" in extract_text(str(archive))
        assert p.get_page_count(scan_pdf, "application/pdf") == 1
        assert p.get_thumbnail(scan_pdf, "application/pdf").exists()
        assert paperless_stubs["thumbnail"][-1] == archive
        assert p.get_date() is None
        tempdir = p._tempdir
    assert not tempdir.exists()
    assert fake_client.uploads[0] == (scan_pdf, "scan.pdf")


def test_content_format_text(paperless_stubs, fake_client, invoice, monkeypatch):
    monkeypatch.setenv("PAPERLESS_MINERU_CONTENT_FORMAT", "text")
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf")
        assert p.get_text().startswith("Rechnung Nr. 2026-0815\n\nMusterfirma")
        assert "##" not in p.get_text()


def test_born_digital_pdf_without_archive_skips_mineru(paperless_stubs, fake_client, invoice):
    paperless_stubs["born_digital"] = ("Bereits vorhandener Text", True)
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf", produce_archive=False)
        assert p.get_text() == "Bereits vorhandener Text"
        assert p.get_archive_path() is None
    assert fake_client.uploads == []


def test_scanned_pdf_without_archive_still_ocrs(paperless_stubs, fake_client, invoice):
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf", produce_archive=False)
        assert "Rechnung" in p.get_text()
        assert p.get_archive_path() is None
    assert len(fake_client.uploads) == 1


def test_image_is_wrapped_into_pdf(paperless_stubs, fake_client, invoice):
    png, _ = invoice
    with MineruDocumentParser() as p:
        p.parse(png, "image/png")
        uploaded, filename = fake_client.uploads[0]
        assert uploaded.suffix == ".pdf" and filename == "scan.pdf"
        import pikepdf

        with pikepdf.open(uploaded) as pdf:
            # 1654 px at 200 dpi -> A4 width in points
            assert float(pdf.pages[0].mediabox[2]) == pytest.approx(595.44, abs=0.5)
        archive = p.get_archive_path()
        assert "Musterfirma GmbH" in extract_text(str(archive))
        assert p.get_page_count(png, "image/png") == 1


def test_rgba_image_without_dpi(paperless_stubs, fake_client, tmp_path):
    from PIL import Image

    img = tmp_path / "alpha.png"
    Image.new("RGBA", (1240, 1754), (255, 255, 255, 255)).save(img)
    with MineruDocumentParser() as p:
        p.parse(img, "image/png")
        import pikepdf

        with pikepdf.open(fake_client.uploads[0][0]) as pdf:
            # no DPI -> estimated from A4 width (149 dpi, integer-rounded)
            assert float(pdf.pages[0].mediabox[2]) == pytest.approx(595, abs=6)


def test_pages_with_text_are_not_overlaid(paperless_stubs, fake_client, invoice, monkeypatch):
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        monkeypatch.setattr(p, "_pages_with_text", lambda pdf: {0})
        p.parse(scan_pdf, "application/pdf")
        assert extract_text(str(p.get_archive_path())).strip() == ""


def test_pages_with_text_detection(paperless_stubs, fake_client, invoice):
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        assert p._pages_with_text(scan_pdf) == set()
        p.parse(scan_pdf, "application/pdf")
        assert p._pages_with_text(p.get_archive_path()) == {0}


def test_no_geometry_means_no_archive(paperless_stubs, fake_client, invoice):
    fake_client.result = MineruResult(markdown="nur Text", middle_json=None, api="legacy")
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf")
        assert p.get_text() == "nur Text"
        assert p.get_archive_path() is None


def test_text_layer_disabled(paperless_stubs, fake_client, invoice, monkeypatch):
    monkeypatch.setenv("PAPERLESS_MINERU_TEXT_LAYER", "false")
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf")
        assert p.get_archive_path() is None


def test_mineru_failure_raises_parse_error(paperless_stubs, fake_client, invoice):
    fake_client.result = MineruError("server down")
    _, scan_pdf = invoice
    with MineruDocumentParser() as p, pytest.raises(FakeParseError, match="server down"):
        p.parse(scan_pdf, "application/pdf")


def test_pdfa_conversion_uses_ocrmypdf_without_ocr(paperless_stubs, fake_client, invoice, monkeypatch):
    calls = []

    def fake_ocr(src, dst, **kwargs):
        calls.append(kwargs)
        dst.write_bytes(src.read_bytes())

    import ocrmypdf

    monkeypatch.setattr(ocrmypdf, "ocr", fake_ocr)
    monkeypatch.setattr(FakeOcrConfig, "output_type", "pdfa-2")
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf")
        assert p.get_archive_path().name == "archive.pdf"
    assert calls == [
        {"output_type": "pdfa-2", "skip_text": True, "ocr_engine": "none", "progress_bar": False},
    ]


def test_pdfa_failure_keeps_text_layer_pdf(paperless_stubs, fake_client, invoice, monkeypatch):
    import ocrmypdf

    def boom(*a, **k):
        raise RuntimeError("ghostscript missing")

    monkeypatch.setattr(ocrmypdf, "ocr", boom)
    monkeypatch.setattr(FakeOcrConfig, "output_type", "pdfa")
    _, scan_pdf = invoice
    with MineruDocumentParser() as p:
        p.parse(scan_pdf, "application/pdf")
        assert p.get_archive_path().name == "archive-textlayer.pdf"
