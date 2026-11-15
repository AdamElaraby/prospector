FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PROSPECTOR_DATA_DIR=/data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home prospector \
    && install -d -o prospector -g prospector /data

COPY config.yaml .env.example ./
COPY prospector/ ./prospector/
USER prospector
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "prospector.hosted"]
