"""MinerU client against a mocked HTTP API (both server generations)."""

from __future__ import annotations

import json

import httpx
import pytest

from paperless_mineru.client import MineruClient, MineruError
from paperless_mineru.config import MineruSettings


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(monkeypatch, handler, **env) -> tuple[MineruClient, FakeClock, list[httpx.Request]]:
    monkeypatch.setenv("PAPERLESS_MINERU_ENDPOINT", "http://mineru.test:8000")
    for k, v in env.items():
        monkeypatch.setenv(f"PAPERLESS_MINERU_{k}", v)
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    clock = FakeClock()
    client = MineruClient(
        MineruSettings.from_env(),
        transport=httpx.MockTransport(recording),
        sleep=clock.sleep,
        clock=clock,
    )
    return client, clock, seen


def multipart_fields(request: httpx.Request) -> dict[str, list[str]]:
    """Tiny multipart parser for the form fields (not the file part)."""
    body = request.read()
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    fields: dict[str, list[str]] = {}
    for part in body.split(b"--" + boundary):
        if b"name=" not in part or b"filename=" in part:
            continue
        head, _, value = part.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        fields.setdefault(name, []).append(value.rstrip(b"\r\n").decode())
    return fields


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


# ----------------------------------------------------------------------
# Legacy (MinerU 2.x / 3.x)
# ----------------------------------------------------------------------


def test_legacy_async_flow(monkeypatch, pdf, legacy_result):
    polls = iter(["pending", "processing", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "healthy", "version": "3.4.0"})
        if path == "/tasks" and request.method == "POST":
            return httpx.Response(202, json={"task_id": "t-1", "status": "pending"})
        if path == "/tasks/t-1":
            return httpx.Response(200, json={"task_id": "t-1", "status": next(polls)})
        if path == "/tasks/t-1/result":
            return httpx.Response(200, json=legacy_result)
        return httpx.Response(404)

    client, clock, seen = make_client(
        monkeypatch, handler, BACKEND="hybrid-engine", EFFORT="medium", LANG="ch"
    )
    result = client.parse(pdf)

    assert result.api == "legacy"
    assert result.server_version == "3.4.0"
    assert result.markdown.startswith("## Rechnung Nr. 2026-0815")
    assert result.middle_json["pdf_info"][0]["page_size"] == [595, 842]
    assert clock.sleeps == [2.0, 2.0]

    submit = next(r for r in seen if r.url.path == "/tasks")
    fields = multipart_fields(submit)
    assert fields["backend"] == ["hybrid-engine"]
    assert fields["effort"] == ["medium"]
    assert fields["lang_list"] == ["ch"]
    assert fields["return_middle_json"] == ["true"]
    assert fields["parse_method"] == ["auto"]
    assert b'filename="doc.pdf"' in submit.read()
    assert "authorization" not in submit.headers


def test_legacy_defaults_omit_backend(monkeypatch, pdf, legacy_result):
    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t"})
        if request.url.path == "/tasks/t":
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, json=legacy_result)

    client, _, seen = make_client(monkeypatch, handler, API="legacy")
    client.parse(pdf)
    fields = multipart_fields(next(r for r in seen if r.url.path == "/tasks"))
    assert "backend" not in fields and "effort" not in fields
    assert not any(r.url.path.endswith("health") for r in seen)


def test_legacy_task_failure(monkeypatch, pdf):
    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t"})
        return httpx.Response(200, json={"status": "failed", "error": "CUDA out of memory"})

    client, _, _ = make_client(monkeypatch, handler, API="legacy")
    with pytest.raises(MineruError, match="CUDA out of memory"):
        client.parse(pdf)


def test_legacy_timeout(monkeypatch, pdf):
    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t"})
        return httpx.Response(200, json={"status": "processing"})

    client, clock, _ = make_client(monkeypatch, handler, API="legacy", TIMEOUT="10")
    with pytest.raises(MineruError, match="did not finish within 10s"):
        client.parse(pdf)
    assert clock.now >= 10


def test_legacy_sync_fallback_for_old_servers(monkeypatch, pdf, legacy_result):
    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path == "/file_parse":
            return httpx.Response(200, json=legacy_result)
        return httpx.Response(500)

    client, _, seen = make_client(monkeypatch, handler, API="legacy")
    assert client.parse(pdf).markdown
    assert [r.url.path for r in seen] == ["/tasks", "/file_parse"]


def test_submit_retries_when_busy(monkeypatch, pdf, legacy_result):
    answers = iter([httpx.Response(503), httpx.Response(202, json={"task_id": "t"})])

    def handler(request):
        if request.url.path == "/tasks":
            return next(answers)
        if request.url.path == "/tasks/t":
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, json=legacy_result)

    client, clock, _ = make_client(monkeypatch, handler, API="legacy")
    client.parse(pdf)
    assert clock.sleeps[0] == 2.0


def test_poll_survives_transient_errors(monkeypatch, pdf, legacy_result):
    statuses = iter([httpx.Response(502), httpx.Response(200, json={"status": "completed"})])

    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t"})
        if request.url.path == "/tasks/t":
            return next(statuses)
        return httpx.Response(200, json=legacy_result)

    client, _, _ = make_client(monkeypatch, handler, API="legacy")
    assert client.parse(pdf).markdown


def test_api_key_sent_as_bearer(monkeypatch, pdf, legacy_result):
    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t"})
        if request.url.path == "/tasks/t":
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, json=legacy_result)

    client, _, seen = make_client(monkeypatch, handler, API="legacy", API_KEY="s3cret")
    client.parse(pdf)
    assert all(r.headers["authorization"] == "Bearer s3cret" for r in seen)


def test_unreachable_server(monkeypatch, pdf):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client, _, _ = make_client(monkeypatch, handler)
    with pytest.raises(MineruError, match="unreachable"):
        client.parse(pdf)


# ----------------------------------------------------------------------
# V1 (MinerU >= 4.0)
# ----------------------------------------------------------------------


def test_v1_detect_and_flow(monkeypatch, pdf, v1_middle):
    results = iter(
        [
            httpx.Response(202, json={"task_id": "job_1", "status": "running"}),
            httpx.Response(
                200,
                json={
                    "task_id": "job_1",
                    "status": "completed",
                    "files": [
                        {
                            "status": "completed",
                            "content": {"markdown": "# Rechnung", "middle_json": v1_middle},
                        },
                    ],
                },
            ),
        ],
    )

    def handler(request):
        path = request.url.path
        if path == "/health":
            return httpx.Response(404)
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": "job_1", "status": "queued"})
        if path == "/v1/tasks/job_1/result":
            return next(results)
        return httpx.Response(404)

    client, _, seen = make_client(monkeypatch, handler, TIER="standard", OCR_MODE="ocr")
    result = client.parse(pdf)

    assert result.api == "v1"
    assert result.markdown == "# Rechnung"
    assert result.middle_json["schema"] == "docvortex.middle"
    assert result.server_version == "4.0.12"
    submit = next(r for r in seen if r.url.path == "/v1/tasks")
    assert submit.url.params.get_list("output_formats") == ["markdown", "middle_json"]
    assert submit.url.params["tier"] == "standard"
    assert submit.url.params["ocr_mode"] == "ocr"


def test_v1_failed_task(monkeypatch, pdf):
    def handler(request):
        if request.url.path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": "job_1"})
        return httpx.Response(
            409,
            json={"status": "failed", "files": [{"status": "failed", "error": {"message": "bad pdf"}}]},
        )

    client, _, _ = make_client(monkeypatch, handler, API="v1")
    with pytest.raises(MineruError, match="bad pdf"):
        client.parse(pdf)


def test_v1_error_envelope_on_submit(monkeypatch, pdf):
    def handler(request):
        return httpx.Response(
            413,
            json={"error": {"type": "invalid_request_error", "message": "File exceeds 200 MiB"}},
        )

    client, _, _ = make_client(monkeypatch, handler, API="v1")
    with pytest.raises(MineruError, match="File exceeds 200 MiB"):
        client.parse(pdf)


def test_v1_middle_json_as_string(monkeypatch, pdf, v1_middle):
    def handler(request):
        if request.url.path == "/v1/tasks":
            return httpx.Response(202, json={"task_id": "j"})
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "status": "completed",
                        "content": {"markdown": "x", "middle_json": json.dumps(v1_middle)},
                    }
                ]
            },
        )

    client, _, _ = make_client(monkeypatch, handler, API="v1")
    assert client.parse(pdf).middle_json["pages"][0]["page_idx"] == 0
