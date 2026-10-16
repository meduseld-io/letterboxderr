FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY letterboxderr.py web.py entrypoint.sh ./
COPY static/ static/

VOLUME ["/data"]

ENV STATE_FILE=/data/state.json \
    USERS_FILE=/data/users.json \
    WEB_PORT=8484 \
    WEB_HOST=0.0.0.0 \
    PUID=1000 \
    PGID=1000

EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('WEB_PORT', '8484'), timeout=5)"

ENTRYPOINT ["/app/entrypoint.sh"]
