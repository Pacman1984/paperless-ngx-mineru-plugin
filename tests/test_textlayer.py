import json

import pikepdf
import pytest
from conftest import INVOICE_LINES
from pdfminer.high_level import extract_pages, extract_text
from pdfminer.layout import LTChar, LTTextContainer, LTTextLine

from paperless_mineru.layout import TextItem, extract_items
from paperless_mineru.textlayer import add_text_layer, wrap_block


def _lines(pdf_path):
    out = []
    for page in extract_pages(str(pdf_path)):
        for el in page:
            if isinstance(el, LTTextContainer):
                out.extend(line for line in el if isinstance(line, LTTextLine))
    return out


def _chars(pdf_path):
    def walk(obj):
        if isinstance(obj, LTChar):
            yield obj
        for child in getattr(obj, "_objs", []) or []:
            yield from walk(child)

    return [c for page in extract_pages(str(pdf_path)) for c in walk(page)]


def test_invoice_text_layer_matches_scan(invoice, legacy_result, tmp_path):
    _, scan_pdf = invoice
    assert not extract_text(str(scan_pdf)).strip()  # image only

    items = extract_items(json.loads(legacy_result["results"]["scan"]["middle_json"]))
    out = tmp_path / "out.pdf"
    assert add_text_layer(scan_pdf, items, out) == 1

    text = extract_text(str(out))
    for line in ["Rechnung Nr. 2026-0815", *INVOICE_LINES]:
        assert " ".join(line.split()) in " ".join(text.split())

    # Line boxes must sit where MinerU saw the text (PDF y axis is bottom-up).
    with pikepdf.open(scan_pdf) as pdf:
        page_h = float(pdf.pages[0].mediabox[3])
    by_text = {line.get_text().strip(): line.bbox for line in _lines(out)}
    x0, y0, x1, y1 = by_text["Musterfirma GmbH, Beispielstraße 12, 12345 Musterstadt"]
    sx, sy = 595.44 / 595, page_h / 842
    assert x0 == pytest.approx(50 * sx, abs=1.5)
    assert x1 == pytest.approx(458 * sx, abs=1.5)
    assert y1 == pytest.approx(page_h - 128 * sy, abs=1.5)
    assert y0 == pytest.approx(page_h - 149 * sy, abs=1.5)


def test_original_content_unchanged_and_text_invisible(invoice, legacy_result, tmp_path):
    _, scan_pdf = invoice
    items = extract_items(json.loads(legacy_result["results"]["scan"]["middle_json"]))
    out = tmp_path / "out.pdf"
    add_text_layer(scan_pdf, items, out)
    with pikepdf.open(out) as pdf:
        page = pdf.pages[0]
        ops = pikepdf.unparse_content_stream(pikepdf.parse_content_stream(page))
        assert ops.startswith(b"q\n")  # original wrapped in q ... Q
        assert b"3 Tr" in ops
        assert len(page.get_images()) == 1  # scan image still there
        font = next(iter(page.resources.Font.values()))
        assert font.Subtype == "/Type0" and font.Encoding == "/Identity-H"
        assert "/ToUnicode" in font


def _blank_pdf(path, rotate=0, mediabox=(0, 0, 595, 842)):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(mediabox[2], mediabox[3]))
    if rotate:
        pdf.pages[0].obj.Rotate = rotate
    pdf.save(path)
    return path


@pytest.mark.parametrize(
    ("rotate", "expected"),
    [
        # bbox x 0.1-0.5, y 0.1-0.15 of the *displayed* page. pdfminer applies
        # /Rotate too, so it reports where a viewer shows the text (y bottom-up).
        (0, (59.5, 715.7, 297.5, 757.8)),
        (90, (84.2, 505.75, 421.0, 535.5)),  # displayed page is 842 x 595
        (180, (59.5, 715.7, 297.5, 757.8)),
        (270, (84.2, 505.75, 421.0, 535.5)),
    ],
)
def test_rotation_mapping(tmp_path, rotate, expected):
    src = _blank_pdf(tmp_path / "in.pdf", rotate=rotate)
    out = tmp_path / "out.pdf"
    add_text_layer(src, [TextItem(0, (0.1, 0.1, 0.5, 0.15), "Hallo Welt", True, 1)], out)
    chars = _chars(out)
    assert "".join(c.get_text() for c in chars) == "Hallo Welt"
    xs = [v for c in chars for v in (c.bbox[0], c.bbox[2])]
    ys = [v for c in chars for v in (c.bbox[1], c.bbox[3])]
    ex0, ey0, ex1, ey1 = expected
    assert min(xs) == pytest.approx(ex0, abs=2)
    assert max(xs) == pytest.approx(ex1, abs=2)
    assert min(ys) == pytest.approx(ey0, abs=2)
    assert max(ys) == pytest.approx(ey1, abs=2)


def test_cropbox_offset(tmp_path):
    src = tmp_path / "in.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(700, 900))
    pdf.pages[0].obj.CropBox = [50, 40, 645, 882]
    pdf.save(src)
    out = tmp_path / "out.pdf"
    add_text_layer(src, [TextItem(0, (0.0, 0.0, 1.0, 0.05), "Kopfzeile", True, 1)], out)
    chars = _chars(out)
    assert min(c.bbox[0] for c in chars) == pytest.approx(50, abs=1)
    assert max(c.bbox[3] for c in chars) == pytest.approx(882, abs=1)


def test_skip_pages_and_non_bmp(tmp_path):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.add_blank_page()
    src = tmp_path / "in.pdf"
    pdf.save(src)
    out = tmp_path / "out.pdf"
    items = [
        TextItem(0, (0.1, 0.1, 0.9, 0.2), "erste", True, 1),
        TextItem(1, (0.1, 0.1, 0.9, 0.2), "zweite \U0001f600 Ölmühle", True, 2),
    ]
    assert add_text_layer(src, items, out, skip_pages={0}) == 1
    texts = [extract_text(str(out), page_numbers=[i]).strip() for i in range(2)]
    assert texts[0] == ""
    assert texts[1] == "zweite � Ölmühle"


def test_block_level_items_wrap_inside_bbox(tmp_path):
    src = _blank_pdf(tmp_path / "in.pdf")
    out = tmp_path / "out.pdf"
    body = "Sehr geehrte Damen und Herren, für die gelieferte Ware erlauben wir uns, " * 3
    add_text_layer(src, [TextItem(0, (0.1, 0.2, 0.9, 0.3), body.strip(), False, 1)], out)
    lines = _lines(out)
    assert len(lines) >= 2
    for line in lines:
        x0, y0, x1, y1 = line.bbox
        assert x0 >= 0.1 * 595 - 1 and x1 <= 0.9 * 595 + 1
        assert y0 >= 842 * 0.7 - 1 and y1 <= 842 * 0.8 + 1
    assert " ".join(extract_text(str(out)).split()) == " ".join(body.split())


def test_wrap_block():
    assert wrap_block("", 100, 10) == []
    one = wrap_block("kurz", 400, 12)
    assert one == ["kurz"]
    many = wrap_block("a " * 200, 200, 200)
    assert len(many) > 3
    assert wrap_block("Zeile eins\nZeile zwei", 400, 30) == ["Zeile eins", "Zeile zwei"]
