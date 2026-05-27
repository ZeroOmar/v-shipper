# Multi-stage build for slim image
FROM python:3.11-alpine as builder

# Install build dependencies
RUN apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev \
    linux-headers

# Copy requirements
COPY requirements.txt .

# Install Python dependencies into the shared runtime site-packages
RUN pip install --no-cache-dir -r requirements.txt

# Clean up build dependencies
RUN apk del .build-deps


# Final runtime stage
FROM alpine:3.20

# Install runtime dependencies
RUN apk add --no-cache \
    python3 \
    rsync \
    openssh-client \
    ca-certificates \
    tzdata

# Create non-root user
RUN addgroup -g 1000 appuser && \
    adduser -D -u 1000 -G appuser appuser

# Set working directory
WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local /usr/local

# Copy application
COPY app/ /app/

# Create necessary directories
RUN mkdir -p /tmp/locks /config && \
    chown -R appuser:appuser /tmp/locks /config

# Set environment variables
ENV PATH=/usr/local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run application as root so it can work with root-owned files
USER root

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:80/api/health').read()" || exit 1

# Expose port
EXPOSE 80

# Run application
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
