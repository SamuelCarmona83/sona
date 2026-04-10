FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py poc_setlistfm.py ./
COPY src/ ./src/

# .env and .cache are mounted at runtime via docker-compose volumes
CMD ["python3", "bot.py"]
