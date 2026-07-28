FROM python:3.11-slim

WORKDIR /app

COPY apps/api/requirements.txt ./apps/api/
COPY apps/worker/requirements.txt ./apps/worker/
RUN pip install --no-cache-dir -r ./apps/api/requirements.txt -r ./apps/worker/requirements.txt

COPY . .

# Ensure we can import all modules from project root
ENV PYTHONPATH=/app

CMD ["python", "-m", "apps.worker.main"]
