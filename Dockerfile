FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-noto-cjk \
        pandoc \
        weasyprint \
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
