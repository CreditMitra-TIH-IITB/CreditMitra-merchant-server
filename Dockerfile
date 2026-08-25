FROM python:3.11-slim

# Security: run as non-root
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY data/india_merchants.json ./data/

# The SQLite enrichment cache is written at runtime, so the directory has to
# be writable by the non-root user.
RUN chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
