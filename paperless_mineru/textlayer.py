"""Write an invisible, searchable text layer onto PDF pages with pikepdf.

The text is drawn in render mode 3 (invisible) with a glyphless CID font
(Identity-H, CID == Unicode BMP code point, identity ToUnicode CMap), the same
technique Tesseract and OCRmyPDF use. Every glyph is 500/1000 em wide and the
horizontal scaling (``Tz``) stretches each line to its bbox, so selection and
search highlights line up with the scanned text.

Coordinates arrive as fractions of the displayed page (origin top-left) and
are mapped into PDF user space, honouring the page's CropBox and /Rotate.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from importlib import resources
from pathlib import Path

import pikepdf

from paperless_mineru.layout import TextItem

logger = logging.getLogger("paperless.parsing.mineru")

_GLYPH_WIDTH = 0.5  # em, must match /DW below
_ASCENT = 0.8  # baseline sits at 80% of the line height
_LINE_SPACING = 1.15


def _font_program() -> bytes | None:
    """OCRmyPDF's glyphless Occulta font, if available (embedded for viewers)."""
    try:
        return resources.files("ocrmypdf.data").joinpath("Occulta.ttf").read_bytes()
    except Exception:  # pragma: no cover - ocrmypdf is always present in Paperless
        return None


def _font_gid() -> int:
    """Glyph id with a 500-unit advance inside Occulta (``blank1``)."""
    return 4


def _to_unicode_cmap() -> bytes:
    ranges = [f"<{hi:02X}00> <{hi:02X}FF> <{hi:02X}00>" for hi in range(256) if not 0xD8 <= hi <= 0xDF]
    chunks = []
    for i in range(0, len(ranges), 100):
        part = ranges[i : i + 100]
        chunks.append(f"{len(part)} beginbfrange\n" + "\n".join(part) + "\nendbfrange")
    body = "\n".join(chunks)
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{body}\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")


def _make_font(pdf: pikepdf.Pdf) -> pikepdf.Object:
    gid = _font_gid()
    # Every CID maps to the same blank 500-unit glyph.
    cid_to_gid = pdf.make_stream(gid.to_bytes(2, "big") * 65536)
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name("/MinerUGlyphLess"),
        Flags=5,
        FontBBox=[0, -200, 500, 800],
        ItalicAngle=0,
        Ascent=800,
        Descent=-200,
        CapHeight=800,
        StemV=80,
    )
    program = _font_program()
    if program:
        font_file = pdf.make_stream(program)
        descriptor[pikepdf.Name.FontFile2] = font_file
    cid_font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name("/MinerUGlyphLess"),
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String("Adobe"),
            Ordering=pikepdf.String("Identity"),
            Supplement=0,
        ),
        FontDescriptor=pdf.make_indirect(descriptor),
        DW=int(_GLYPH_WIDTH * 1000),
        CIDToGIDMap=cid_to_gid,
    )
    to_unicode = pdf.make_stream(_to_unicode_cmap())
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type0,
        BaseFont=pikepdf.Name("/MinerUGlyphLess"),
        Encoding=pikepdf.Name("/Identity-H"),
        DescendantFonts=[pdf.make_indirect(cid_font)],
        ToUnicode=to_unicode,
    )
    return pdf.make_indirect(font)


def _encode(text: str) -> str:
    """Identity-H hex string; non-BMP characters become U+FFFD."""
    return "".join(
        f"{ord(c) if ord(c) <= 0xFFFF and not 0xD800 <= ord(c) <= 0xDFFF else 0xFFFD:04X}" for c in text
    )


def wrap_block(text: str, width: float, height: float) -> list[str]:
    """Split block text into lines so it fills a ``width`` x ``height`` box.

    Used for block-level geometry: estimates how many lines the block has
    from its aspect ratio and the glyph width, then word-wraps evenly.
    """
    paragraphs = [" ".join(p.split()) for p in text.splitlines()]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return []
    n = sum(len(p) for p in paragraphs)
    # n chars of width 0.5*lh on k lines of height lh*spacing:
    #   k * (width / (0.5*lh)) >= n  and  k * lh * spacing = height
    k = max(len(paragraphs), round(math.sqrt(n * _GLYPH_WIDTH * height * _LINE_SPACING / max(width, 1e-6))))
    target = max(1, math.ceil(n / k))
    lines: list[str] = []
    for paragraph in paragraphs:
        current = ""
        for word in paragraph.split(" "):
            if current and len(current) + 1 + len(word) > target:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}" if current else word
        if current:
            lines.append(current)
    return lines


class _PageGeometry:
    """Maps displayed-page fractions (top-left origin) to PDF user space."""

    def __init__(self, page: pikepdf.Page) -> None:
        box = page.obj.get("/CropBox") or page.mediabox
        llx, lly, urx, ury = (float(v) for v in box)
        self.llx, self.lly = min(llx, urx), min(lly, ury)
        self.urx, self.ury = max(llx, urx), max(lly, ury)
        self.rotate = int(page.obj.get("/Rotate", 0)) % 360
        w, h = self.urx - self.llx, self.ury - self.lly
        self.disp_w, self.disp_h = (h, w) if self.rotate in (90, 270) else (w, h)

    def point(self, fx: float, fy: float) -> tuple[float, float]:
        X, Y = fx * self.disp_w, fy * self.disp_h
        if self.rotate == 90:
            return self.llx + Y, self.lly + X
        if self.rotate == 180:
            return self.urx - X, self.lly + Y
        if self.rotate == 270:
            return self.urx - Y, self.ury - X
        return self.llx + X, self.ury - Y

    def basis(self) -> tuple[float, float, float, float]:
        """Unit vectors (dir_x, dir_y, up_x, up_y) of displayed text in user space."""
        return {
            0: (1, 0, 0, 1),
            90: (0, 1, -1, 0),
            180: (-1, 0, 0, -1),
            270: (0, -1, 1, 0),
        }.get(self.rotate, (1, 0, 0, 1))


def _line_ops(geo: _PageGeometry, font: str, text: str, x0: float, y0: float, x1: float, y1: float) -> str:
    """Content stream operators for one line inside fractional bbox."""
    width = (x1 - x0) * geo.disp_w
    height = (y1 - y0) * geo.disp_h
    if not text or width <= 0 or height <= 0:
        return ""
    size = height
    natural = len(text) * _GLYPH_WIDTH * size
    hscale = 100.0 * width / natural if natural > 0 else 100.0
    bx, by = geo.point(x0, y0 + _ASCENT * (y1 - y0))
    dx, dy, ux, uy = geo.basis()
    return (
        f"BT 3 Tr /{font} {size:.2f} Tf {hscale:.2f} Tz "
        f"{dx} {dy} {ux} {uy} {bx:.2f} {by:.2f} Tm <{_encode(text)}> Tj ET\n"
    )


def page_ops(geo: _PageGeometry, font: str, items: Iterable[TextItem]) -> str:
    ops: list[str] = []
    for item in items:
        x0, y0, x1, y1 = item.bbox
        if item.single_line:
            ops.append(_line_ops(geo, font, " ".join(item.text.split()), x0, y0, x1, y1))
            continue
        width = (x1 - x0) * geo.disp_w
        height = (y1 - y0) * geo.disp_h
        lines = wrap_block(item.text, width, height)
        if not lines:
            continue
        step = (y1 - y0) / len(lines)
        line_h = step / _LINE_SPACING
        for i, line in enumerate(lines):
            top = y0 + i * step
            ops.append(_line_ops(geo, font, line, x0, top, x1, top + line_h))
    return "".join(ops)


def add_text_layer(
    source: Path,
    items: list[TextItem],
    output: Path,
    *,
    skip_pages: set[int] | None = None,
) -> int:
    """Write ``source`` plus invisible text to ``output``; returns pages touched."""
    skip_pages = skip_pages or set()
    by_page: dict[int, list[TextItem]] = {}
    for item in items:
        by_page.setdefault(item.page_idx, []).append(item)

    touched = 0
    with pikepdf.open(source) as pdf:
        font = None
        for page_idx, page in enumerate(pdf.pages):
            page_items = by_page.get(page_idx)
            if not page_items or page_idx in skip_pages:
                continue
            if font is None:
                font = _make_font(pdf)
            name = page.add_resource(font, pikepdf.Name.Font, prefix="FMinerU")
            ops = page_ops(_PageGeometry(page), str(name).lstrip("/"), page_items)
            if not ops:
                continue
            # Isolate the original content's graphics state from ours.
            page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
            page.contents_add(pdf.make_stream(b"\nQ\n" + ops.encode("ascii")))
            touched += 1
        pdf.save(output)
    logger.debug("Text layer written to %d page(s) of %s", touched, output)
    return touched
