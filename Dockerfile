FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY letterboxderr.py .
COPY web.py .
COPY static/ static/

VOLUME ["/data"]

ENV STATE_FILE=/data/state.json
ENV USERS_FILE=/data/users.json
ENV WEB_PORT=8484
ENV WEB_HOST=0.0.0.0

EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8484/api/health', timeout=5).raise_for_status()"

ENTRYPOINT ["python", "-u", "web.py"]
