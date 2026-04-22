FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    flask \
    gunicorn \
    requests \
    playwright \
    && for i in 1 2 3; do \
         python -m playwright install --with-deps chromium && break; \
         if [ "$i" -eq 3 ]; then exit 1; fi; \
         echo "playwright install failed, retrying..." >&2; \
         sleep 5; \
       done

COPY umasn2api.py /app/
COPY umans2api /app/umans2api
COPY templates /app/templates

EXPOSE 8787

CMD ["gunicorn", "-w", "2", "--threads", "8", "-b", "0.0.0.0:8787", "--timeout", "300", "umasn2api:app"]
