FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY sql ./sql
COPY data ./data

EXPOSE 8000
# Puerto dinámico: Render/Railway inyectan $PORT; local usa 8000.
CMD ["sh", "-c", "python -m scripts.import_rep_faqs data/rep_faqs_full.md || true; uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
