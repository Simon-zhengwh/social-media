FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/tmp/social-media

WORKDIR /app
RUN groupadd --gid 10001 social && useradd --uid 10001 --gid social --create-home social
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/scripts/__init__.py ./scripts/__init__.py
COPY backend/scripts/maintenance.py ./scripts/maintenance.py
RUN mkdir -p /tmp/social-media && chown -R social:social /app /tmp/social-media
USER social:social
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
