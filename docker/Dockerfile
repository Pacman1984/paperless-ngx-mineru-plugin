# Paperless-ngx with the MinerU parser plugin baked in.
#
# Build from a local checkout (context = repository root):
#   docker build -f docker/Dockerfile -t paperless-ngx-mineru:3.3.1 .
#
# Or straight from GitHub without cloning:
#   docker build -f docker/Dockerfile \
#     -t paperless-ngx-mineru:3.3.1 \
#     "https://github.com/Pacman1984/paperless-ngx-mineru-plugin.git#main"
#
# Pin PAPERLESS_TAG to the version you run; the plugin needs >= 3.0.
ARG PAPERLESS_TAG=3.3.1
FROM ghcr.io/paperless-ngx/paperless-ngx:${PAPERLESS_TAG}

COPY pyproject.toml README.md LICENSE /tmp/paperless-mineru/
COPY paperless_mineru /tmp/paperless-mineru/paperless_mineru

# --no-deps: httpx, pikepdf, img2pdf and pillow already ship with Paperless;
# never let the plugin change Paperless' own pinned dependencies.
RUN uv pip install --system --no-cache --no-deps --python "$(command -v python3)" /tmp/paperless-mineru \
    && rm -rf /tmp/paperless-mineru \
    && python3 -c "import paperless_mineru; print('paperless-mineru', paperless_mineru.__version__)"
