FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip && rm -rf /var/lib/apt/lists/*

# Install deno for yt-dlp JavaScript extraction (required for modern YouTube)
# Downloads the binary directly from GitHub releases for reliability
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then DENO_ARCH="aarch64"; else DENO_ARCH="x86_64"; fi && \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}-unknown-linux-gnu.zip" -o /tmp/deno.zip && \
    unzip /tmp/deno.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/deno && \
    rm /tmp/deno.zip && \
    deno --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download yt-dlp EJS challenge solver so it's available at runtime without network calls
RUN yt-dlp --skip-download --remote-components ejs:github "https://www.youtube.com/watch?v=dQw4w9WgXcQ" || true

COPY bot.py poc_setlistfm.py ./
COPY src/ ./src/

# .env and .cache are mounted at runtime via docker-compose volumes
CMD ["python3", "bot.py"]
