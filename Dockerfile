FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY dc-app/ /app
WORKDIR /app

# PYTHON dependencies
RUN pip install --upgrade pip &&\
    pip install -r requirements.txt --break-system-packages

CMD ["python3", "discord-bot.py"]