# Single image, single entrypoint (single-service MVP — no FSRS job).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jolt ./jolt

# Build identifier reported by GET /health (CI passes the git SHA).
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home jolt
USER 10001

EXPOSE 8000

# Container Apps ingress terminates TLS and forwards to Uvicorn on 8000;
# --proxy-headers makes request URLs report the original https scheme and host.
CMD ["uvicorn", "jolt.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
