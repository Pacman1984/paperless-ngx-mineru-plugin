"""Environment-driven configuration for the MinerU parser.

Everything is read from ``PAPERLESS_MINERU_*`` environment variables so the
plugin can be configured in ``docker-compose.env`` next to the regular
Paperless settings. This module deliberately has no Django imports so it can
be unit-tested in isolation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("paperless.parsing.mineru")

API_AUTO: Final = "auto"
API_V1: Final = "v1"  # MinerU >= 4.0: /v1/file_parse, /v1/tasks
API_LEGACY: Final = "legacy"  # MinerU 2.x/3.x: /file_parse, /tasks

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}

# Tesseract language codes (PAPERLESS_OCR_LANGUAGE) -> MinerU pipeline OCR
# language. MinerU's "ch" model also covers English and Latin scripts
# (German, French, ...), so it is the fallback. Only the pipeline/hybrid
# backends use this value; VLM backends ignore it.
_TESSERACT_TO_MINERU_LANG: Final[dict[str, str]] = {
    "rus": "east_slavic",
    "ukr": "east_slavic",
    "bel": "east_slavic",
    "srp": "cyrillic",
    "bul": "cyrillic",
    "mkd": "cyrillic",
    "kaz": "cyrillic",
    "mon": "cyrillic",
    "ara": "arabic",
    "fas": "arabic",
    "urd": "arabic",
    "kor": "korean",
    "ell": "el",
    "tha": "th",
    "tam": "ta",
    "tel": "te",
    "kan": "ka",
    "hin": "devanagari",
    "mar": "devanagari",
    "nep": "devanagari",
}


def _env_str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    logger.warning("Invalid boolean %s=%r, using default %s.", name, raw, default)
    return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid number %s=%r, using default %s.", name, raw, default)
        return default
    return max(value, minimum)


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer %s=%r, using default %d.", name, raw, default)
        return default


def _env_choice(name: str, choices: tuple[str, ...], default: str | None) -> str | None:
    raw = _env_str(name)
    if raw is None:
        return default
    if raw not in choices:
        logger.warning(
            "Invalid value %s=%r (valid: %s), using default %r.",
            name,
            raw,
            ", ".join(choices),
            default,
        )
        return default
    return raw


def mineru_lang_from_tesseract(ocr_language: str | None) -> str:
    """Map a Tesseract language string like ``deu+eng`` to a MinerU language."""
    for code in (ocr_language or "").replace(",", "+").split("+"):
        mapped = _TESSERACT_TO_MINERU_LANG.get(code.strip().lower())
        if mapped:
            return mapped
    return "ch"


@dataclass(frozen=True, slots=True)
class MineruSettings:
    endpoint: str | None
    api_key: str | None
    api: str
    timeout: float
    connect_timeout: float
    poll_interval: float
    # Legacy (2.x/3.x) request options; None means "server default".
    backend: str | None
    effort: str | None
    lang: str
    formula_enable: bool
    table_enable: bool
    # V1 (4.x) request options.
    tier: str | None
    # Shared.
    ocr_mode: str
    content_format: str
    score: int
    text_layer: bool
    pdfa: bool
    verify_ssl: bool

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    @classmethod
    def from_env(cls) -> MineruSettings:
        endpoint = _env_str("PAPERLESS_MINERU_ENDPOINT")
        return cls(
            endpoint=endpoint.rstrip("/") if endpoint else None,
            api_key=_env_str("PAPERLESS_MINERU_API_KEY"),
            api=_env_choice("PAPERLESS_MINERU_API", (API_AUTO, API_V1, API_LEGACY), API_AUTO) or API_AUTO,
            timeout=_env_float("PAPERLESS_MINERU_TIMEOUT", 900.0, minimum=1.0),
            connect_timeout=_env_float("PAPERLESS_MINERU_CONNECT_TIMEOUT", 10.0, minimum=1.0),
            poll_interval=_env_float("PAPERLESS_MINERU_POLL_INTERVAL", 2.0, minimum=0.1),
            backend=_env_str("PAPERLESS_MINERU_BACKEND"),
            effort=_env_choice("PAPERLESS_MINERU_EFFORT", ("medium", "high"), None),
            lang=_env_str("PAPERLESS_MINERU_LANG")
            or mineru_lang_from_tesseract(_env_str("PAPERLESS_OCR_LANGUAGE")),
            formula_enable=_env_bool("PAPERLESS_MINERU_FORMULA_ENABLE", True),
            table_enable=_env_bool("PAPERLESS_MINERU_TABLE_ENABLE", True),
            tier=_env_choice(
                "PAPERLESS_MINERU_TIER",
                ("flash", "basic", "standard", "advanced"),
                None,
            ),
            ocr_mode=_env_choice("PAPERLESS_MINERU_OCR_MODE", ("auto", "txt", "ocr"), "auto") or "auto",
            content_format=_env_choice(
                "PAPERLESS_MINERU_CONTENT_FORMAT",
                ("markdown", "text"),
                "markdown",
            )
            or "markdown",
            score=_env_int("PAPERLESS_MINERU_SCORE", 20),
            text_layer=_env_bool("PAPERLESS_MINERU_TEXT_LAYER", True),
            pdfa=_env_bool("PAPERLESS_MINERU_PDFA", True),
            verify_ssl=_env_bool("PAPERLESS_MINERU_VERIFY_SSL", True),
        )
