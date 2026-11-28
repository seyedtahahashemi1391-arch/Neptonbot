FROM python:3.14.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    NEPTON_DB_PATH=/tmp/nepton_state.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY bot.py gunicorn.conf.py ./
RUN useradd --create-home --uid 10001 nepton \
    && chown -R nepton:nepton /app

USER 10001:10001

EXPOSE 10000

CMD ["sh", "-c", "gunicorn --config gunicorn.conf.py --workers 1 --bind 0.0.0.0:${PORT:-10000} bot:app"]
