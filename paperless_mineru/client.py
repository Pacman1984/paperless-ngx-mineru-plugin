"""HTTP client for self-hosted MinerU servers.

Two server generations are supported:

* ``legacy`` - MinerU 2.x/3.x ``mineru-api`` (``/health``, ``/tasks``,
  ``/tasks/{id}/result``, ``/file_parse``). Multipart form options such as
  ``backend``, ``parse_method`` and ``lang_list``; the result carries
  ``md_content`` and a ``middle_json`` string with line-level bboxes.
* ``v1`` - MinerU >= 4.0 ``mineru-kit api-server`` (``/v1/health``,
  ``/v1/tasks``, ``/v1/tasks/{id}/result``). Query options ``tier`` and
  ``ocr_mode``; ``middle_json`` is a ``docvortex.middle`` document with
  normalized block-level bboxes.

Both are driven asynchronously (submit, poll, fetch) so long documents and
the first request after a vLLM cold start do not hit HTTP read timeouts.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from paperless_mineru.config import API_AUTO, API_LEGACY, API_V1, MineruSettings

logger = logging.getLogger("paperless.parsing.mineru")

_LEGACY_TERMINAL_OK = {"completed", "done", "success", "succeeded"}
_LEGACY_TERMINAL_FAIL = {"failed", "error", "canceled", "cancelled"}
_V1_TERMINAL_FAIL = {"failed", "canceled"}
_MAX_TRANSIENT_ERRORS = 5

# Cache of detected API flavour per endpoint for the lifetime of the worker.
_detected_api: dict[str, str] = {}


class MineruError(Exception):
    """Raised for any failure talking to MinerU or interpreting its answer."""


@dataclass(frozen=True, slots=True)
class MineruResult:
    markdown: str
    middle_json: dict[str, Any] | None
    api: str
    server_version: str | None = None


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"HTTP {response.status_code}: {err['message']}"
        for key in ("error", "detail", "message"):
            if body.get(key):
                return f"HTTP {response.status_code}: {body[key]}"
    return f"HTTP {response.status_code}: {str(body)[:300]}"


class MineruClient:
    def __init__(
        self,
        settings: MineruSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not settings.endpoint:
            raise MineruError("PAPERLESS_MINERU_ENDPOINT is not configured")
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        headers = {"Accept": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        self._http = httpx.Client(
            base_url=settings.endpoint,
            headers=headers,
            timeout=httpx.Timeout(min(settings.timeout, 120.0), connect=settings.connect_timeout),
            verify=settings.verify_ssl,
            transport=transport,
            # Never follow redirects with credentials to other hosts.
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MineruClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_api(self) -> str:
        """Return ``legacy`` or ``v1``; honours an explicit setting."""
        if self.settings.api != API_AUTO:
            return self.settings.api
        cached = _detected_api.get(self.settings.endpoint or "")
        if cached:
            return cached
        api = self._probe_api()
        _detected_api[self.settings.endpoint or ""] = api
        logger.info("MinerU server at %s speaks the %s API.", self.settings.endpoint, api)
        return api

    def parse(self, path: Path, *, filename: str | None = None) -> MineruResult:
        api = self.detect_api()
        filename = filename or path.name
        try:
            if api == API_V1:
                return self._parse_v1(path, filename)
            return self._parse_legacy(path, filename)
        except httpx.HTTPError as e:
            raise MineruError(f"MinerU request failed: {e.__class__.__name__}: {e}") from e

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _probe_api(self) -> str:
        try:
            r = self._http.get("/health")
            if r.status_code == 200:
                version = str(r.json().get("version", ""))
                if version[:2] in ("2.", "3."):
                    return API_LEGACY
            r = self._http.get("/v1/health")
            if r.status_code == 200:
                return API_V1
            r = self._http.get("/health")
            if r.status_code == 200:
                return API_LEGACY
        except (httpx.HTTPError, ValueError) as e:
            raise MineruError(f"MinerU server at {self.settings.endpoint} unreachable: {e}") from e
        raise MineruError(
            f"Could not detect MinerU API at {self.settings.endpoint} "
            "(neither /health nor /v1/health answered 200)",
        )

    # ------------------------------------------------------------------
    # Legacy (MinerU 2.x / 3.x)
    # ------------------------------------------------------------------

    def _legacy_form(self) -> dict[str, Any]:
        s = self.settings
        form: dict[str, Any] = {
            "lang_list": s.lang,
            "parse_method": s.ocr_mode,
            "formula_enable": str(s.formula_enable).lower(),
            "table_enable": str(s.table_enable).lower(),
            "return_md": "true",
            "return_middle_json": "true",
            "return_content_list": "false",
            "return_images": "false",
            "return_model_output": "false",
            "response_format_zip": "false",
        }
        if s.backend:
            form["backend"] = s.backend
        if s.effort:
            form["effort"] = s.effort
        return form

    def _files(self, path: Path, filename: str) -> dict[str, Any]:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {"files": (filename, path.read_bytes(), mime)}

    def _parse_legacy(self, path: Path, filename: str) -> MineruResult:
        deadline = self._clock() + self.settings.timeout
        r = self._post_with_retry("/tasks", data=self._legacy_form(), files=self._files(path, filename))
        if r.status_code == 404:
            # Very old 2.x servers only have the synchronous endpoint.
            logger.debug("MinerU has no /tasks endpoint, using synchronous /file_parse")
            r = self._http.post(
                "/file_parse",
                data=self._legacy_form(),
                files=self._files(path, filename),
                timeout=httpx.Timeout(self.settings.timeout, connect=self.settings.connect_timeout),
            )
            if r.status_code != 200:
                raise MineruError(f"MinerU /file_parse failed: {_error_message(r)}")
            return self._legacy_result(r.json())
        if r.status_code not in (200, 202):
            raise MineruError(f"MinerU task submission failed: {_error_message(r)}")
        task_id = r.json().get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise MineruError(f"MinerU returned no task_id: {r.text[:300]}")
        logger.debug("MinerU task %s submitted", task_id)

        self._wait_legacy(task_id, deadline)

        r = self._get_tolerant(f"/tasks/{task_id}/result", deadline)
        if r.status_code != 200:
            raise MineruError(f"MinerU task {task_id} result failed: {_error_message(r)}")
        return self._legacy_result(r.json())

    def _wait_legacy(self, task_id: str, deadline: float) -> None:
        while True:
            r = self._get_tolerant(f"/tasks/{task_id}", deadline)
            if r.status_code != 200:
                raise MineruError(f"MinerU task {task_id} status failed: {_error_message(r)}")
            body = r.json()
            status = str(body.get("status", "")).lower()
            if status in _LEGACY_TERMINAL_OK:
                return
            if status in _LEGACY_TERMINAL_FAIL:
                raise MineruError(f"MinerU task {task_id} {status}: {body.get('error') or body}")
            if self._clock() >= deadline:
                raise MineruError(
                    f"MinerU task {task_id} did not finish within "
                    f"{self.settings.timeout:.0f}s (last status: {status or 'unknown'})",
                )
            self._sleep(self.settings.poll_interval)

    @staticmethod
    def _legacy_result(body: dict[str, Any]) -> MineruResult:
        results = body.get("results")
        if not isinstance(results, dict) or not results:
            raise MineruError(f"MinerU result contains no documents: {str(body)[:300]}")
        doc = next(iter(results.values()))
        markdown = doc.get("md_content") or ""
        middle = doc.get("middle_json")
        if isinstance(middle, str):
            try:
                middle = json.loads(middle) if middle.strip() else None
            except ValueError:
                logger.warning("MinerU returned an unparsable middle_json, ignoring it")
                middle = None
        return MineruResult(
            markdown=markdown,
            middle_json=middle if isinstance(middle, dict) else None,
            api=API_LEGACY,
            server_version=body.get("version"),
        )

    # ------------------------------------------------------------------
    # V1 (MinerU >= 4.0)
    # ------------------------------------------------------------------

    def _parse_v1(self, path: Path, filename: str) -> MineruResult:
        deadline = self._clock() + self.settings.timeout
        params: list[tuple[str, str]] = [
            ("ocr_mode", self.settings.ocr_mode),
            ("output_formats", "markdown"),
            ("output_formats", "middle_json"),
        ]
        if self.settings.tier:
            params.append(("tier", self.settings.tier))
        r = self._post_with_retry("/v1/tasks", params=params, files=self._files(path, filename))
        if r.status_code not in (200, 202):
            raise MineruError(f"MinerU task submission failed: {_error_message(r)}")
        task_id = r.json().get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise MineruError(f"MinerU returned no task_id: {r.text[:300]}")
        logger.debug("MinerU task %s submitted", task_id)

        while True:
            r = self._get_tolerant(f"/v1/tasks/{task_id}/result", deadline)
            if r.status_code == 200:
                return self._v1_result(r.json())
            if r.status_code == 409:
                body = r.json()
                raise MineruError(
                    f"MinerU task {task_id} {body.get('status', 'failed')}: "
                    f"{self._v1_file_error(body) or body.get('error') or ''}",
                )
            if r.status_code != 202:
                raise MineruError(f"MinerU task {task_id} result failed: {_error_message(r)}")
            if self._clock() >= deadline:
                self._http.delete(f"/v1/tasks/{task_id}")
                raise MineruError(
                    f"MinerU task {task_id} did not finish within {self.settings.timeout:.0f}s",
                )
            self._sleep(self.settings.poll_interval)

    @staticmethod
    def _v1_file_error(body: dict[str, Any]) -> str | None:
        for f in body.get("files") or []:
            if f.get("error"):
                err = f["error"]
                return err.get("message") if isinstance(err, dict) else str(err)
        return None

    @classmethod
    def _v1_result(cls, body: dict[str, Any]) -> MineruResult:
        files = body.get("files") or []
        if not files:
            raise MineruError(f"MinerU result contains no files: {str(body)[:300]}")
        f = files[0]
        if f.get("status") != "completed":
            raise MineruError(f"MinerU could not parse the document: {cls._v1_file_error(body)}")
        content = f.get("content") or {}
        middle = content.get("middle_json")
        if isinstance(middle, str):
            middle = json.loads(middle)
        producer = ((middle or {}).get("metadata") or {}).get("producer") or {}
        return MineruResult(
            markdown=content.get("markdown") or "",
            middle_json=middle if isinstance(middle, dict) else None,
            api=API_V1,
            server_version=producer.get("version"),
        )

    # ------------------------------------------------------------------
    # Transport helpers
    # ------------------------------------------------------------------

    def _post_with_retry(self, url: str, **kwargs: Any) -> httpx.Response:
        """Submit with a few retries for connection errors and 502/503/429."""
        delay = 2.0
        for attempt in range(3):
            try:
                r = self._http.post(url, **kwargs)
            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                if attempt == 2:
                    raise
                logger.warning("MinerU submit failed (%s), retrying in %.0fs", e, delay)
            else:
                if r.status_code not in (429, 502, 503) or attempt == 2:
                    return r
                logger.warning("MinerU busy (HTTP %d), retrying in %.0fs", r.status_code, delay)
            self._sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_tolerant(self, url: str, deadline: float) -> httpx.Response:
        """GET that rides out short network hiccups while polling."""
        errors = 0
        while True:
            try:
                r = self._http.get(url)
            except httpx.TransportError as e:
                errors += 1
                if errors >= _MAX_TRANSIENT_ERRORS or self._clock() >= deadline:
                    raise
                logger.warning("MinerU poll failed (%s), retrying", e)
            else:
                if r.status_code not in (502, 503, 504):
                    return r
                errors += 1
                if errors >= _MAX_TRANSIENT_ERRORS or self._clock() >= deadline:
                    return r
            self._sleep(self.settings.poll_interval)
