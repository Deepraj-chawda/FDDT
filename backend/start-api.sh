#!/bin/sh
set -e
# Apply migrations, ensure the seeded admin exists, then serve the API.
alembic upgrade head
python seed.py
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
