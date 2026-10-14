#!/usr/bin/env bash
# Install the paperless-mineru parser plugin at container start.
#
# Mount this directory to /custom-cont-init.d (read-only is fine). The script
# runs as root before Paperless starts and installs, in this order:
#   1. a wheel placed next to it (paperless_mineru-*.whl), or
#   2. PAPERLESS_MINERU_PIP_SPEC, e.g.
#      git+https://github.com/Pacman1984/paperless-ngx-mineru-plugin.git@v0.1.0
# It is idempotent: an already installed matching version is left alone.
set -euo pipefail

log() { echo "[paperless-mineru-init] $*"; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
wheel="$(ls -1 "${script_dir}"/paperless_mineru-*.whl 2>/dev/null | sort -V | tail -n1 || true)"
spec="${wheel:-${PAPERLESS_MINERU_PIP_SPEC:-}}"

if [[ -z "${spec}" ]]; then
    log "No wheel in ${script_dir} and PAPERLESS_MINERU_PIP_SPEC unset - skipping."
    exit 0
fi

installed="$(python3 -c 'import paperless_mineru as m; print(m.__version__)' 2>/dev/null || true)"
if [[ -n "${wheel}" && -n "${installed}" && "$(basename "${wheel}")" == paperless_mineru-"${installed}"-* ]]; then
    log "paperless-mineru ${installed} already installed."
    exit 0
fi

log "Installing ${spec}"
if command -v uv >/dev/null 2>&1; then
    uv pip install --system --no-cache --no-deps --python "$(command -v python3)" "${spec}"
else
    python3 -m pip install --no-cache-dir --no-deps "${spec}"
fi
log "Installed paperless-mineru $(python3 -c 'import paperless_mineru as m; print(m.__version__)')"
