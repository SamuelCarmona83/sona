FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py poc_setlistfm.py ./

# .env and .cache are mounted at runtime via docker-compose volumes
CMD ["python3", "bot.py"]
