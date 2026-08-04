# Atajos operativos. Requiere .env con DATABASE_URL, etc.
include .env
export

.PHONY: install migrate backfill import-faqs import-pdfs seed run

install:
	pip install -r requirements.txt --break-system-packages

migrate:
	psql "$(DATABASE_URL)" -f sql/001_init_pgvector.sql
	psql "$(DATABASE_URL)" -f sql/002_kb_faqs_analytics.sql
	psql "$(DATABASE_URL)" -f sql/003_promotions.sql

backfill:
	python -m scripts.backfill_catalog

import-faqs:
	python -m scripts.import_rep_faqs data/rep_faqs_full.md

import-pdfs:
	python -m scripts.import_pdfs data/pdfs

seed: backfill import-faqs import-pdfs

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
