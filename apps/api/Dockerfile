FROM python:3.11-slim

WORKDIR /app

COPY apps/api/requirements.txt ./apps/api/
COPY apps/worker/requirements.txt ./apps/worker/
RUN pip install --no-cache-dir -r ./apps/api/requirements.txt -r ./apps/worker/requirements.txt

COPY . .

# Immutable image provenance; set by scripts/with-build-info.sh at build time.
ARG CANGZHI_VERSION=unversioned
ARG CANGZHI_REVISION=unknown
ARG CANGZHI_BUILD_TIME=unknown
ARG CANGZHI_BUILD_DIRTY=true
ENV CANGZHI_VERSION=${CANGZHI_VERSION} \
    CANGZHI_REVISION=${CANGZHI_REVISION} \
    CANGZHI_BUILD_TIME=${CANGZHI_BUILD_TIME} \
    CANGZHI_BUILD_DIRTY=${CANGZHI_BUILD_DIRTY}
LABEL org.opencontainers.image.version=${CANGZHI_VERSION} \
      org.opencontainers.image.revision=${CANGZHI_REVISION} \
      org.opencontainers.image.created=${CANGZHI_BUILD_TIME}

# Ensure we can import all modules from project root
ENV PYTHONPATH=/app

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
