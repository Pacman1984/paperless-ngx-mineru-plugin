import json

from paperless_mineru.layout import clean_markdown, extract_items, html_to_text, items_to_text


def test_legacy_items_are_line_level(legacy_result):
    middle = json.loads(legacy_result["results"]["scan"]["middle_json"])
    items = extract_items(middle)
    assert len(items) == 8
    assert all(i.single_line and i.page_idx == 0 for i in items)
    assert items[0].text == "Rechnung Nr. 2026-0815"
    assert items[1].text == "Musterfirma GmbH, Beispielstraße 12, 12345 Musterstadt"
    # bbox normalized by page_size [595, 842]
    x0, y0, x1, y1 = items[1].bbox
    assert round(x0 * 595) == 50 and round(y0 * 842) == 128
    assert round(x1 * 595) == 458 and round(y1 * 842) == 149


def test_v1_items_are_block_level(v1_middle):
    items = extract_items(v1_middle)
    texts = [i.text for i in items]
    assert texts == [
        "Rechnung Nr. 2026-0815",
        "Sehr geehrte Damen und Herren, für die gelieferte Ware erlauben wir uns, "
        "folgenden Betrag in Rechnung zu stellen. Siehe Portal.",
        "Positionen",
        "Artikel | Preis\nSchraube | 1,00 EUR",
        "Seite zwei",
    ]
    assert not any(i.single_line for i in items)
    # caption inherits the parent bbox, the body has its own
    assert items[2].bbox == (0.09, 0.3, 0.9, 0.4)
    assert items[3].bbox == (0.09, 0.32, 0.9, 0.4)
    assert items[-1].page_idx == 1


def test_unknown_or_empty_middle_json():
    assert extract_items(None) == []
    assert extract_items({"foo": 1}) == []


def test_html_to_text():
    assert html_to_text("<table><tr><td>a</td><td> b &amp; c </td></tr><tr><td>d</td></tr></table>") == (
        "a | b & c\nd"
    )
    assert html_to_text("<p>just  text</p>") == "just text"


def test_clean_markdown():
    md = "# T\n\n![](images/abc.jpg)\n\n\n\n<table><tr><td>x</td><td>y</td></tr></table>\n\nEnde"
    assert clean_markdown(md) == "# T\n\nx | y\n\nEnde"


def test_items_to_text(legacy_result, v1_middle):
    middle = json.loads(legacy_result["results"]["scan"]["middle_json"])
    text = items_to_text(extract_items(middle))
    assert text.startswith("Rechnung Nr. 2026-0815\n\nMusterfirma GmbH")
    assert "Gesamtbetrag: 1.234,56 EUR" in text
    assert items_to_text(extract_items(v1_middle)).endswith("Schraube | 1,00 EUR\n\nSeite zwei")
