"""Paperless-ngx parser plugin that offloads OCR / document parsing to MinerU.

Flow for one document:

1. Born-digital PDF and no archive wanted -> use the PDF's own text, no
   remote call (same shortcut as Paperless' built-in remote OCR parser).
2. Images are wrapped into a PDF locally (img2pdf), so MinerU coordinates and
   the archive pages share one geometry.
3. The PDF is sent to MinerU (async task + polling). Markdown becomes the
   document content; ``middle_json`` provides bboxes.
4. An invisible text layer is written onto every page that has no text yet,
   then the result is optionally converted to PDF/A with OCRmyPDF using
   ``ocr_engine=none`` (Ghostscript only - Tesseract never runs).

Paperless keeps everything else: consumption, thumbnails, date detection,
classification, storage.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Self

from paperless_mineru import __version__
from paperless_mineru.client import MineruClient, MineruError
from paperless_mineru.config import MineruSettings
from paperless_mineru.layout import clean_markdown, extract_items, items_to_text

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from paperless.parsers import MetadataEntry, ParserContext

logger = logging.getLogger("paperless.parsing.mineru")

_SUPPORTED_MIME_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# A page with at least this many non-blank characters already has a text
# layer and gets no additional invisible text (avoids duplicate hits).
_PAGE_TEXT_MIN_CHARS = 20


def _parse_error(message: str) -> Exception:
    from documents.parsers import ParseError

    return ParseError(message)


class MineruDocumentParser:
    """Remote OCR via a self-hosted MinerU server, with searchable archive PDF."""

    name: str = "Paperless-ngx MinerU Parser"
    version: str = __version__
    author: str = "paperless-mineru contributors"
    url: str = "https://github.com/Pacman1984/paperless-ngx-mineru-plugin"

    # Documents leave the Paperless host. Paperless only offers this parser
    # when remote OCR is allowed (PAPERLESS_REMOTE_OCR_MODE=always, the
    # default, or a workflow "Remote OCR" action with mode workflow_only).
    uses_remote_service: bool = True

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        return _SUPPORTED_MIME_TYPES

    @classmethod
    def score(cls, mime_type: str, filename: str, path: Path | None = None) -> int | None:
        if mime_type not in _SUPPORTED_MIME_TYPES:
            return None
        settings = MineruSettings.from_env()
        if not settings.enabled:
            return None
        # 20 beats Tesseract (10) and ties with the built-in Azure parser,
        # where third-party parsers win.
        return settings.score

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def can_produce_archive(self) -> bool:
        return True

    @property
    def requires_pdf_rendition(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, logging_group: object = None) -> None:
        from django.conf import settings as django_settings

        django_settings.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        self._tempdir = Path(tempfile.mkdtemp(prefix="paperless-mineru-", dir=django_settings.SCRATCH_DIR))
        self._logging_group = logging_group
        self._settings = MineruSettings.from_env()
        self._text: str | None = None
        self._archive_path: Path | None = None
        self._image_pdf: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        logger.debug("Cleaning up temporary directory %s", self._tempdir)
        shutil.rmtree(self._tempdir, ignore_errors=True)

    def configure(self, context: ParserContext) -> None:
        pass

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse(self, document_path: Path, mime_type: str, *, produce_archive: bool = True) -> None:
        document_path = Path(document_path)

        if mime_type == "application/pdf" and not produce_archive:
            from paperless.parsers.utils import pdf_born_digital_text

            text, born_digital = pdf_born_digital_text(document_path, log=logger)
            if born_digital:
                logger.debug("MinerU: skipped - born-digital PDF and no archive requested")
                self._text = text or ""
                return

        source_pdf = document_path if mime_type == "application/pdf" else self._image_to_pdf(document_path)

        try:
            with MineruClient(self._settings) as client:
                result = client.parse(source_pdf, filename=f"{document_path.stem}.pdf")
        except MineruError as e:
            logger.error("MinerU parsing failed: %s", e)
            raise _parse_error(f"MinerU parsing failed: {e}") from e

        logger.info(
            "MinerU (%s API%s) parsed %s",
            result.api,
            f", v{result.server_version}" if result.server_version else "",
            document_path.name,
        )

        items = extract_items(result.middle_json)
        if self._settings.content_format == "text" and items:
            text = items_to_text(items)
        else:
            text = clean_markdown(result.markdown)
        from paperless.parsers.utils import post_process_text

        self._text = (post_process_text(text) if self._settings.content_format == "text" else text) or ""

        if not produce_archive:
            return
        if not self._settings.text_layer:
            logger.debug("MinerU: text layer disabled, no archive produced")
            return
        if not items:
            logger.warning(
                "MinerU returned no layout geometry (middle_json); "
                "content is stored but no searchable archive PDF is produced.",
            )
            return
        self._archive_path = self._build_archive(source_pdf, items, mime_type)

    def _build_archive(self, source_pdf: Path, items: list, mime_type: str) -> Path:
        from paperless_mineru.textlayer import add_text_layer

        skip = self._pages_with_text(source_pdf) if mime_type == "application/pdf" else set()
        layered = self._tempdir / "archive-textlayer.pdf"
        try:
            touched = add_text_layer(source_pdf, items, layered, skip_pages=skip)
        except Exception as e:
            raise _parse_error(f"Could not write text layer: {e}") from e
        logger.debug("MinerU: text layer on %d page(s), %d page(s) already had text", touched, len(skip))
        return self._maybe_pdfa(layered)

    def _maybe_pdfa(self, pdf: Path) -> Path:
        """Convert to PDF/A per PAPERLESS_OCR_OUTPUT_TYPE without running OCR."""
        if not self._settings.pdfa:
            return pdf
        try:
            from paperless.config import OcrConfig

            ocr_config = OcrConfig()
            output_type = str(ocr_config.output_type)
        except Exception:  # pragma: no cover - defensive, config always exists
            return pdf
        if "pdfa" not in output_type:
            return pdf

        import ocrmypdf

        out = self._tempdir / "archive.pdf"
        kwargs = {
            "output_type": output_type,
            "skip_text": True,
            "ocr_engine": "none",
            "progress_bar": False,
        }
        if ocr_config.color_conversion_strategy:
            kwargs["color_conversion_strategy"] = str(ocr_config.color_conversion_strategy)
        try:
            ocrmypdf.ocr(pdf, out, **kwargs)
            return out
        except Exception as e:
            logger.warning("PDF/A conversion failed (%s), keeping plain PDF with text layer.", e)
            return pdf

    def _pages_with_text(self, pdf: Path) -> set[int]:
        try:
            from pdfminer.high_level import extract_pages
            from pdfminer.layout import LTTextContainer
        except ImportError:  # pragma: no cover
            return set()
        pages: set[int] = set()
        try:
            for idx, page in enumerate(extract_pages(str(pdf))):
                chars = sum(
                    len("".join(el.get_text().split())) for el in page if isinstance(el, LTTextContainer)
                )
                if chars >= _PAGE_TEXT_MIN_CHARS:
                    pages.add(idx)
        except Exception as e:
            logger.warning("Could not inspect existing PDF text (%s); assuming none.", e)
            return set()
        return pages

    def _image_to_pdf(self, image: Path) -> Path:
        """Wrap an image into a PDF with sensible DPI (Paperless semantics)."""
        if self._image_pdf is not None:
            return self._image_pdf
        import img2pdf
        from PIL import Image

        src = image
        with Image.open(image) as im:
            dpi = None
            with contextlib.suppress(KeyError, TypeError, IndexError, ValueError):
                dpi = round(im.info["dpi"][0]) or None
            needs_convert = im.mode in ("RGBA", "LA", "P", "PA") or (
                getattr(im, "n_frames", 1) > 1 and im.format == "GIF"
            )
            if needs_convert or im.format not in ("JPEG", "PNG", "TIFF", "JPEG2000"):
                src = self._tempdir / "image-flat.png"
                im.convert("RGB").save(src, **({"dpi": (dpi, dpi)} if dpi else {}))
            width = im.size[0]
        if not dpi:
            try:
                from paperless.config import OcrConfig

                dpi = OcrConfig().image_dpi
            except Exception:  # pragma: no cover
                dpi = None
        if not dpi:
            dpi = max(72, int(width / (21 / 2.54)))  # assume A4 width
        out = self._tempdir / "image.pdf"
        layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
        try:
            out.write_bytes(img2pdf.convert(str(src), layout_fun=layout, rotation=img2pdf.Rotation.ifvalid))
        except Exception as e:
            raise _parse_error(f"Could not convert image to PDF: {e}") from e
        self._image_pdf = out
        return out

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    def get_text(self) -> str:
        return self._text or ""

    def get_date(self) -> datetime.datetime | None:
        return None

    def get_archive_path(self) -> Path | None:
        return self._archive_path

    # ------------------------------------------------------------------
    # Thumbnail, page count, metadata
    # ------------------------------------------------------------------

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        from documents.parsers import make_thumbnail_from_pdf

        if self._archive_path is not None:
            pdf = self._archive_path
        elif mime_type == "application/pdf":
            pdf = Path(document_path)
        else:
            pdf = self._image_to_pdf(Path(document_path))
        return make_thumbnail_from_pdf(pdf, self._tempdir, self._logging_group)

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        from paperless.parsers.utils import get_page_count_for_pdf

        if mime_type == "application/pdf":
            return get_page_count_for_pdf(Path(document_path), log=logger)
        if self._archive_path is not None:
            return get_page_count_for_pdf(self._archive_path, log=logger)
        return None

    def extract_metadata(self, document_path: Path, mime_type: str) -> list[MetadataEntry]:
        if mime_type != "application/pdf":
            return []
        from paperless.parsers.utils import extract_pdf_metadata

        return extract_pdf_metadata(Path(document_path), log=logger)
