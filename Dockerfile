FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps chromium
RUN chmod -R a+rX /opt/playwright

COPY ./app ./app

RUN groupadd -r relicta && useradd -r -g relicta -d /app -s /sbin/nologin relicta \
    && mkdir -p /app/data \
    && chown -R relicta:relicta /app

USER relicta

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]