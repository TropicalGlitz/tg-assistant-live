"""Parser del dataset de FAQs (data/rep_faqs_full.md) → registros estructurados.
Sin dependencias externas para poder testearlo y reutilizarlo en el importador.
"""
from __future__ import annotations

import re
from pathlib import Path

ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|(.+?)\|(.+?)\|\s*(\d+)\s*\|(.+?)\|\s*$")


def parse_faq_md(path: str) -> list[dict]:
    """Devuelve FAQs únicas (fusiona duplicados: merge de recomendados, max time_used)."""
    rows: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        _, q, a, used, reco = m.groups()
        q, a = q.strip(), a.strip()
        reco_list = [] if reco.strip() in ("—", "-", "") else [
            s.strip() for s in reco.split(";") if s.strip() and s.strip() != "—"
        ]
        used_i = int(used)
        if q in rows:
            rows[q]["recommended_skus"] = sorted(set(rows[q]["recommended_skus"]) | set(reco_list))
            rows[q]["time_used"] = max(rows[q]["time_used"], used_i)
        else:
            rows[q] = {
                "question": q,
                "answer": a,
                "recommended_skus": reco_list,
                "time_used": used_i,
            }
    # post_action se deriva al final: tras fusionar duplicados, refleja el estado real.
    out = list(rows.values())
    for r in out:
        r["post_action"] = "recommend_product" if r["recommended_skus"] else "offer_assistance"
    return out
