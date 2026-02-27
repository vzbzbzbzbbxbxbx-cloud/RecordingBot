FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app

RUN mkdir -p bot/data/downloads bot/data/tmp bot/data/logs && \
    touch bot/__init__.py bot/utils/__init__.py

CMD ["python", "-m", "bot.main"]
