FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080

WORKDIR /app

ARG WKHTMLTOX_DEB_URL="https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb"
ARG WKHTMLTOX_SHA256="98ba0d157b50d36f23bd0dedf4c0aa28c7b0c50fcdcdc54aa5b6bbba81a3941d"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-noto-cjk \
        pandoc \
        xfonts-75dpi \
        xfonts-base \
    && curl -fsSL "$WKHTMLTOX_DEB_URL" -o /tmp/wkhtmltox.deb \
    && echo "$WKHTMLTOX_SHA256  /tmp/wkhtmltox.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb \
    && rm -f /tmp/wkhtmltox.deb \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --progress-bar off -r requirements.txt

RUN adk_config_dir="$(python -c 'from pathlib import Path; import google.adk.cli; print(Path(google.adk.cli.__file__).parent / "browser" / "assets" / "config")')" \
    && mkdir -p "$adk_config_dir" \
    && chown -R appuser:appuser "$adk_config_dir"

COPY --chown=appuser:appuser english_coach ./english_coach

RUN mkdir -p english_coach/input english_coach/reports english_coach/training_inputs \
    && chown -R appuser:appuser english_coach

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "uvicorn english_coach.web_app:app --host 0.0.0.0 --port ${PORT:-8080}"]
