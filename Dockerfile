# syntax=docker/dockerfile:1
FROM python:3.11-slim

ARG APP_VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_DIR=/data \
    PORT=8080 \
    APP_VERSION=${APP_VERSION}

# Install ffmpeg and build dependencies for native packages (psutil on arm64)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove gcc python3-dev

# Copy source
COPY app ./app
COPY static ./static
COPY README.md ./

# Create data volume for recordings
VOLUME ["/data"]

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
