FROM caddy:2.11.4 AS gateway
FROM ghcr.io/astral-sh/uv:0.11.33 AS uv
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    MPT_DATA_DIR=/data \
    MPT_CONFIG_FILE=/data/config.toml \
    HF_HOME=/data/huggingface \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
    PORT=8080

WORKDIR /MoneyPrinterTurbo
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg fonts-noto-core libasound2 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=gateway /usr/bin/caddy /usr/local/bin/caddy
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --all-extras --no-dev --no-install-project --python /usr/local/bin/python \
    && uv cache clean
COPY app ./app
COPY webui ./webui
COPY resource ./resource
COPY deploy ./deploy
COPY cli.py main.py config.example.toml ./
RUN cp /usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf resource/fonts/ \
    && ln -s /data/storage storage \
    && ln -s /data/models models

# Only the authenticated gateway is exposed. Railway terminates HTTPS upstream.
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "deploy.run"]
