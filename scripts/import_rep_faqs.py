"""Importa las 178 FAQs extraídas de REP (data/rep_faqs_full.md) a la tabla `faqs`.

Parsea la tabla markdown → deduplica por pregunta (merge de recomendados, max time_used)
→ genera embeddings → upsert en pgvector.

Uso:
    python -m scripts.import_rep_faqs data/rep_faqs_full.md
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from app.db.session import AsyncSessionLocal
from app.services import embeddings, kb_store

ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|(.+?)\|(.+?)\|\s*(\d+)\s*\|(.+?)\|\s*$")


def parse_md(path: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        _, q, a, used, reco = m.groups()
        q = q.strip()
        a = a.strip()
        reco_list = [] if reco.strip() in ("—", "-", "") else [
            s.strip() for s in reco.split(";") if s.strip() and s.strip() != "—"
        ]
        used_i = int(used)
        if q in rows:  # dedupe: merge
            rows[q]["recommended_skus"] = sorted(set(rows[q]["recommended_skus"]) | set(reco_list))
            rows[q]["time_used"] = max(rows[q]["time_used"], used_i)
        else:
            rows[q] = {
                "question": q,
                "answer": a,
                "recommended_skus": reco_list,
                "time_used": used_i,
                "post_action": "recommend_product" if reco_list else "offer_assistance",
            }
    return list(rows.values())


async def main(path: str) -> None:
    faqs = parse_md(path)
    print(f"Parsed {len(faqs)} unique FAQs")
    # Embeddings en lote sobre la pregunta (grounding de recuperación).
    vectors = await embeddings.embed_batch([f["question"] for f in faqs])
    async with AsyncSessionLocal() as session:
        for f, vec in zip(faqs, vectors):
            await kb_store.upsert_faq(
                session,
                question=f["question"],
                answer=f["answer"],
                synonyms=[],
                embedding=vec,
                recommended_skus=f["recommended_skus"],
                related_product_id=None,
                post_action=f["post_action"],
                time_used=f["time_used"],
                content_hash=embeddings.content_hash(f["question"] + "|" + f["answer"]),
            )
    print(f"Imported {len(faqs)} FAQs into pgvector.")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "data/rep_faqs_full.md"
    asyncio.run(main(src))
