# Single image, single entrypoint (single-service MVP — no FSRS job).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jolt ./jolt

EXPOSE 8000

# Container Apps ingress terminates TLS; Uvicorn serves the ASGI app.
CMD ["uvicorn", "jolt.main:app", "--host", "0.0.0.0", "--port", "8000"]
