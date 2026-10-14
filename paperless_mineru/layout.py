"""Turn MinerU results into positioned text items and plain document text.

The output of :func:`extract_items` is format-neutral: every item carries the
page index, a bbox as fractions of the *displayed* page (origin top-left,
``0..1``) and the text found there. ``single_line`` tells the text layer
whether the bbox is one text line (legacy MinerU, line-level geometry) or a
whole block that still needs to be wrapped (MinerU 4.x, block-level geometry).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

# Legacy span types whose ``content`` is real reading text.
_LEGACY_TEXT_SPANS = {"text", "inline_equation"}
# V1 block types whose content is not printed page text (VLM image
# descriptions, LaTeX of display equations).
_V1_SKIP_TYPES = {"image_body", "equation", "chart_body"}
_V1_HTML_TYPES = {"table_body"}


@dataclass(frozen=True, slots=True)
class TextItem:
    page_idx: int
    bbox: tuple[float, float, float, float]
    text: str
    single_line: bool
    block: int


class _HtmlText(HTMLParser):
    """Extract table text: cells separated by `` | ``, rows by newlines."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._loose: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._loose.append(data)

    def text(self) -> str:
        lines = [" | ".join(c for c in row if c) for row in self.rows]
        lines = [line for line in lines if line]
        if not lines:
            return " ".join("".join(self._loose).split())
        return "\n".join(lines)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return parser.text()


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


def clean_markdown(markdown: str) -> str:
    """Drop image references and render embedded HTML tables as text."""
    text = _MD_IMAGE.sub("", markdown)
    text = _HTML_TABLE.sub(lambda m: "\n" + html_to_text(m.group(0)) + "\n", text)
    return _MANY_BLANK_LINES.sub("\n\n", text).strip()


def items_to_text(items: list[TextItem]) -> str:
    """Plain text in MinerU reading order: lines, blank line between blocks."""
    out: list[str] = []
    prev: tuple[int, int] | None = None
    for item in items:
        key = (item.page_idx, item.block)
        if prev is not None:
            out.append("\n" if key == prev else "\n\n")
        out.append(item.text)
        prev = key
    return "".join(out).strip()


# ----------------------------------------------------------------------
# Geometry extraction
# ----------------------------------------------------------------------


def extract_items(middle_json: dict[str, Any] | None) -> list[TextItem]:
    if not middle_json:
        return []
    if middle_json.get("schema") == "docvortex.middle" or "pages" in middle_json:
        return _extract_v1(middle_json)
    if "pdf_info" in middle_json:
        return _extract_legacy(middle_json)
    return []


def _clamp_bbox(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float] | None:
    x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    return (x0, y0, x1, y1)


def _valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(v, (int, float)) for v in value)
    )


# --- legacy (MinerU 2.x / 3.x): pdf_info[].para_blocks[].lines[].spans[] ---


def _extract_legacy(middle: dict[str, Any]) -> list[TextItem]:
    items: list[TextItem] = []
    block_no = 0
    for page in middle.get("pdf_info") or []:
        page_idx = int(page.get("page_idx", 0))
        size = page.get("page_size") or []
        if len(size) != 2 or not size[0] or not size[1]:
            continue
        w, h = float(size[0]), float(size[1])
        blocks = list(page.get("para_blocks") or []) + list(page.get("discarded_blocks") or [])
        for block in blocks:
            block_no += 1
            items.extend(_legacy_block(block, page_idx, w, h, block_no))
    return items


def _legacy_block(block: dict[str, Any], page_idx: int, w: float, h: float, block_no: int) -> list[TextItem]:
    items: list[TextItem] = []
    for child in block.get("blocks") or []:
        items.extend(_legacy_block(child, page_idx, w, h, block_no))
    for line in block.get("lines") or []:
        texts: list[str] = []
        for span in line.get("spans") or []:
            span_type = span.get("type")
            if span_type in _LEGACY_TEXT_SPANS and isinstance(span.get("content"), str):
                texts.append(span["content"])
            elif span_type == "table" and isinstance(span.get("html"), str):
                bbox = span.get("bbox") if _valid_bbox(span.get("bbox")) else line.get("bbox")
                if _valid_bbox(bbox):
                    item = _make_item(page_idx, bbox, w, h, html_to_text(span["html"]), False, block_no)
                    if item:
                        items.append(item)
        text = " ".join(" ".join(texts).split())
        if text and _valid_bbox(line.get("bbox")):
            item = _make_item(page_idx, line["bbox"], w, h, text, True, block_no)
            if item:
                items.append(item)
    return items


def _make_item(
    page_idx: int,
    bbox: list[float],
    w: float,
    h: float,
    text: str,
    single_line: bool,
    block_no: int,
) -> TextItem | None:
    if not text.strip():
        return None
    norm = _clamp_bbox(bbox[0] / w, bbox[1] / h, bbox[2] / w, bbox[3] / h)
    if norm is None:
        return None
    return TextItem(page_idx, norm, text.strip(), single_line, block_no)


# --- v1 (MinerU >= 4.0, docvortex.middle): pages[].blocks[] with [0,1] bboxes ---


def _span_text(spans: list[Any]) -> str:
    parts: list[str] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        content = span.get("content")
        if span.get("type") == "hyperlink" and isinstance(content, list):
            parts.append(_span_text(content))
        elif span.get("type") in ("text", "equation_inline", "code_inline") and isinstance(content, str):
            parts.append(content)
    return "".join(parts)


def _is_block_list(content: Any) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(c, dict) for c in content)
        and any(("index" in c or "bbox" in c or "blocks" in c) for c in content)
    )


def _extract_v1(middle: dict[str, Any]) -> list[TextItem]:
    items: list[TextItem] = []
    block_no = 0
    for page in middle.get("pages") or []:
        page_idx = int(page.get("page_idx", 0))
        for block in page.get("blocks") or []:
            block_no += 1
            _v1_block(block, None, page_idx, block_no, items)
    return items


def _v1_block(
    block: dict[str, Any],
    inherited: list[float] | None,
    page_idx: int,
    block_no: int,
    out: list[TextItem],
) -> None:
    btype = block.get("type")
    bbox = block.get("bbox") if _valid_bbox(block.get("bbox")) else inherited
    if btype in _V1_SKIP_TYPES:
        return
    content = block.get("content")
    if _is_block_list(content):
        for child in content:
            _v1_block(child, bbox, page_idx, block_no, out)
        return
    if btype in _V1_HTML_TYPES and isinstance(content, str):
        text = html_to_text(content) if "<" in content else content
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _span_text(content)
    else:
        return
    if not text.strip() or bbox is None:
        return
    norm = _clamp_bbox(*[float(v) for v in bbox])
    if norm is not None:
        out.append(TextItem(page_idx, norm, text.strip(), False, block_no))
