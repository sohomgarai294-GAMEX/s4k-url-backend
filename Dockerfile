# S.4K URL Import backend — includes ffmpeg so video+audio merging works.
FROM python:3.12-slim

# ffmpeg is what lets yt-dlp merge separate video and audio streams (1080p etc.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8080
EXPOSE 8080

# Render/Railway/Fly inject $PORT
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
