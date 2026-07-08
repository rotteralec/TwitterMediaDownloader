FROM python:3.11-slim

# ffmpeg is REQUIRED: GIF tweets are transcoded from X's silent MP4 to a real
# .gif at download time. (yt-dlp also uses it for any stream that needs remuxing.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

#Run as an unprivileged user instead of root
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser

ENV PORT=8000 \
    DAILY_LIMIT=3 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Single worker is fine for personal use. Bump --workers when deployed.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
