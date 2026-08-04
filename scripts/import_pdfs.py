"""Ingesta de los PDFs de la Knowledge Base → tabla `kb_chunks` (pgvector).

Extrae texto con pdfplumber, trocea por ventanas de ~250 palabras con solape,
genera embeddings y hace upsert. Genérico: funciona para los 29 PDFs; corre sobre
todos los .pdf de data/pdfs/.

Uso:
    python -m scripts.import_pdfs data/pdfs
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import sys

import pdfplumber

from app.db.session import AsyncSessionLocal
from app.services import embeddings, kb_store

# time_used conocidos del teardown (para boost de retrieval). Default 0.
TIME_USED = {
    "Recommended Paint Quantity Based on Project Size": 717,
    "Candy Basecoats": 701,
    "Reducers": 376,
    "Metal Flake Requirements Based on Project Size and Desired Coverage": 347,
    "Recommended Tip Sizes Based on Metal Flake Size": 328,
    "Candy and Candy Concentrates": 321,
    "Flake Matched Basecoat": 240,
    "Basecoats": 231,
}

WORDS = 250       # tamaño de chunk
OVERLAP = 40      # solape entre chunks


def extract(path: str) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def chunk(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split(" ")
    if len(words) <= WORDS:
        return [text] if text else []
    out, i = [], 0
    while i < len(words):
        out.append(" ".join(words[i : i + WORDS]))
        i += WORDS - OVERLAP
    return out


async def main(folder: str) -> None:
    files = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    print(f"Found {len(files)} PDFs")
    async with AsyncSessionLocal() as session:
        for f in files:
            doc = os.path.splitext(os.path.basename(f))[0]
            used = TIME_USED.get(doc, 0)
            chunks = chunk(extract(f))
            if not chunks:
                print(f"  ! {doc}: no text")
                continue
            vecs = await embeddings.embed_batch(chunks)
            for idx, (c, v) in enumerate(zip(chunks, vecs)):
                await kb_store.upsert_kb_chunk(
                    session, doc_name=doc, chunk_idx=idx, chunk_text=c,
                    embedding=v, time_used=used,
                    content_hash=embeddings.content_hash(c),
                )
            print(f"  ✓ {doc}: {len(chunks)} chunk(s), used={used}")
    print("KB import complete.")


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "data/pdfs"
    asyncio.run(main(folder))
