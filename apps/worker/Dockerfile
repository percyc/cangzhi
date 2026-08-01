FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-noto-cjk \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY apps/api/requirements.txt ./apps/api/
COPY apps/worker/requirements.txt ./apps/worker/
RUN pip install --no-cache-dir -r ./apps/api/requirements.txt -r ./apps/worker/requirements.txt

COPY . .

# Ensure we can import all modules from project root
ENV PYTHONPATH=/app

CMD ["python", "-m", "apps.worker.main"]
