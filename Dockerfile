FROM python:3.12-slim

# Create non-root user
RUN groupadd -r botuser && useradd -r -g botuser botuser

# procps provides pgrep/ps for the healthcheck
# ffmpeg converts Telegram voice notes (OGG/Opus) to WAV for Gemini STT
RUN apt-get update && apt-get install -y --no-install-recommends procps ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY main.py .
COPY bot/ bot/
COPY scripts/ scripts/

# Create data directory for SQLite (Docker volume inherits ownership from image)
RUN mkdir -p /app/data

# Switch to non-root user
RUN chown -R botuser:botuser /app
USER botuser

# Telegram long-polling doesn't expose ports — no EXPOSE needed

# Health check: verify the process is running
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD pgrep -f "python main.py" || exit 1

STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
