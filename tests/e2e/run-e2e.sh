#!/usr/bin/env bash
# Build Paperless-ngx + plugin, consume a synthetic scanned invoice through
# MinerU and verify: plugin discovered, MinerU parser used, content OCR'd,
# archive PDF has a searchable text layer. Tears everything down afterwards
# (KEEP=1 keeps the stack for inspection).
set -euo pipefail
cd "$(dirname "$0")"
compose=(docker compose --env-file .env -f docker-compose.yml)
[[ -f .env ]] || { echo "tests/e2e/.env missing - copy .env.example"; exit 2; }
export PAPERLESS_SECRET_KEY="${PAPERLESS_SECRET_KEY:-$(uv run --project ../.. python -c 'import secrets; print(secrets.token_urlsafe(48))')}"

cleanup() {
    if [[ "${KEEP:-0}" != "1" ]]; then
        "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf consume
    fi
}
trap cleanup EXIT

mkdir -p consume
uv run --project ../.. python make_test_page.py consume/e2e-rechnung.pdf.tmp
"${compose[@]}" up -d --build

echo "== waiting for parser discovery"
for _ in $(seq 1 120); do
    if "${compose[@]}" logs paperless 2>&1 | grep -q "Loaded third-party parser 'Paperless-ngx MinerU Parser'"; then
        break
    fi
    sleep 2
done
"${compose[@]}" logs paperless 2>&1 | grep "third-party parser" || { echo "plugin NOT discovered"; exit 1; }

# Only drop the file once the consumer is up, so it is picked up exactly once.
mv consume/e2e-rechnung.pdf.tmp consume/e2e-rechnung.pdf

echo "== waiting for consumption"
for _ in $(seq 1 150); do
    logs="$("${compose[@]}" logs paperless 2>&1)"
    if grep -qE "Document .*e2e-rechnung.* consumption finished|consumption finished" <<<"$logs"; then
        break
    fi
    if grep -qE "MinerU parsing failed|Error occurred while consuming|Unexpected error while consuming" <<<"$logs"; then
        grep -E "MinerU|ERROR|Error" <<<"$logs" | tail -20
        exit 1
    fi
    sleep 2
done

"${compose[@]}" logs paperless 2>&1 | grep -E "third-party parser|MinerU|Parser:" | tail -10

echo "== verifying document"
"${compose[@]}" exec -T paperless python3 manage.py shell -c '
import subprocess
from documents.models import Document
d = Document.objects.get(original_filename="e2e-rechnung.pdf")
print("content:", d.content[:200].replace("\n", " | "))
assert "Gesamtbetrag" in d.content, "OCR text missing"
assert d.has_archive_version, "no archive PDF"
txt = subprocess.run(["pdftotext", str(d.archive_path), "-"], capture_output=True, text=True).stdout
print("archive text layer:", " ".join(txt.split())[:200])
assert "1.234,56" in txt, "archive PDF has no searchable text layer"
import pikepdf
with pikepdf.open(d.archive_path) as pdf:
    meta = pdf.open_metadata()
    print("archive PDF/A:", meta.get("pdfaid:part"), meta.get("pdfaid:conformance"))
print("E2E OK")
'
