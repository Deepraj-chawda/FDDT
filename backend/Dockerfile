FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libglib is needed by the OpenCV headless wheel at import time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini seed.py start-api.sh start-worker.sh start-beat.sh ./
COPY app ./app

EXPOSE 8000
# The same image runs the API and the Celery worker; the command is set per service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
