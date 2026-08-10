"""Base de conocimiento automotriz/pintura (semilla) para la tabla `faqs`.

Son respuestas GENERALES de técnica de pintura custom que los clientes preguntan
seguido (¿sirve en calipers?, ¿puedo agregar flake?, ¿sirve en RC cars?, etc.).
Se cargan al arrancar el backend, una sola vez (idempotente por pregunta), usando
el modelo de embeddings local y las credenciales de BD que el backend ya tiene.

Para AGREGAR conocimiento: añade tuplas (pregunta, respuesta) a KNOWLEDGE y
redepliega. Las preguntas nuevas se embeben e insertan; las existentes se
actualizan. Nada más que mantener.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services import embeddings, kb_store

_log = logging.getLogger("kb_seed")

# (pregunta como la haría un cliente, respuesta experta pero concisa y honesta).
# Reglas de contenido: técnica general correcta a nivel automotriz; NO inventar
# precios, química exacta ni afirmar que un producto puntual está "rated" para algo.
KNOWLEDGE: list[tuple[str, str]] = [
    (
        "Can I use the paint on brake calipers?",
        "Brake calipers get very hot, so they need a coating rated for high heat. Standard "
        "custom basecoats, candies and clears are not formulated for brake-caliper "
        "temperatures and can discolor or fail there. For a show look on calipers use a "
        "high-temp caliper paint/clear, prep well (clean, scuff, degrease) and always test "
        "on a small spot first. If you're not sure a specific product is heat-rated, check "
        "with our team before spraying.",
    ),
    (
        "Can I add metal flake or metallic to the paint?",
        "Yes. Metal flake is usually suspended in an intercoat/clear (or a compatible "
        "basecoat) rather than mixed straight into candy. Spray it in light, even coats to "
        "control how heavy the flake looks, then bury it with several clear coats and "
        "wet-sand/buff smooth. Start light and build up — you can always add more.",
    ),
    (
        "Can I use the paint on RC car bodies?",
        "Most RC car bodies are clear Lexan/polycarbonate and are painted on the INSIDE with "
        "polycarbonate-specific paint that stays flexible; regular automotive paint tends to "
        "crack or peel on Lexan. For hard-plastic RC parts (bumpers, wings, chassis) you can "
        "use custom paint if you scuff and use a plastic adhesion promoter first.",
    ),
    (
        "Do candy colors need a base coat?",
        "Yes. Candy is translucent, so the final color and brightness come from the metallic "
        "base underneath it (silver, gold, white, etc.). The same candy over a silver base vs. "
        "a gold base looks completely different, so pick your base to get the tone you want.",
    ),
    (
        "Do I need to reduce or thin the paint before spraying?",
        "It depends on the form. Concentrates and many basecoats need to be reduced before "
        "spraying, while ready-to-spray products and spray cans are already set to go. Follow "
        "the reduction ratio for that product and adjust for temperature — reduce a bit more "
        "in hot weather so it lays down smooth.",
    ),
    (
        "Can I paint over powder coat or existing paint?",
        "Yes, as long as the surface is sound. Scuff it to a uniform dull finish, clean and "
        "degrease thoroughly, and on very slick surfaces use an adhesion promoter. Then seal/"
        "base and clear as normal. Skipping the scuff and degrease is the #1 cause of peeling.",
    ),
    (
        "Do I need a clear coat over candy or flake?",
        "Yes. Clear protects the color, adds depth and gloss, and — with flake — is what you "
        "sand and buff to get a smooth, glassy finish. Candy and flake should always be sealed "
        "under clear.",
    ),
    (
        "Can I use the paint on plastic bumpers or trim (ABS, polypropylene)?",
        "Yes on properly prepped rigid plastics: scuff, clean, and use a plastic adhesion "
        "promoter (bare polypropylene especially needs it). For flexible plastic parts add a "
        "flex additive so the finish moves with the part instead of cracking.",
    ),
    (
        "Can I use the paint on a motorcycle tank or a helmet?",
        "Tanks are a classic use — prep (sand, degrease, primer if bare metal), lay your base, "
        "then candy/graphics, then clear. Helmets can be painted too, but avoid harsh solvents "
        "on the shell and follow the helmet maker's guidance, since some shells react to strong "
        "chemicals.",
    ),
    (
        "Can I use the paint on wheels or rims?",
        "Yes, wheels are a popular custom job. They take brake dust, chips and heat, so prep "
        "thoroughly and finish with a durable clear. Don't coat the brake rotor/hub mating "
        "surfaces, and keep paint off the tire bead area.",
    ),
    (
        "How many coats do I need for full color?",
        "It depends on the color. Candies build color with every coat — more coats = a deeper, "
        "richer tone — while solid basecoats usually cover in a few coats. Spray light, even "
        "coats and check the color as you go so you land exactly where you want it.",
    ),
    (
        "Can I airbrush your paint?",
        "Yes. Reduce it thinner than you would for a spray gun and drop your air pressure; "
        "candies and flakes airbrush really well for fades, tribal and fine graphics. Test your "
        "reduction on scrap first to dial it in.",
    ),
    (
        "What is the difference between basecoat, candy and pearl?",
        "Basecoat is your solid foundation color. Candy is a translucent tint that glows over a "
        "metallic base for that deep custom look. Pearl adds a color-shifting shimmer. They're "
        "often layered together — for example a metallic base, then candy, then a pearl or "
        "flake accent, all under clear.",
    ),
    (
        "How do I get a flake finish in the clear coat?",
        "A common method is to suspend flake in intercoat clear and spray it in light coats over "
        "your base, then bury it with several clear coats and wet-sand/buff flat. The amount of "
        "flake you load controls how heavy the sparkle looks.",
    ),
    (
        "How long should I wait between coats and before buffing?",
        "It depends on what you're spraying. COLOR coats — basecoats, pearl basecoats, flake "
        "bases and candy — need each coat to dry FULLY before the next one goes on; recoating "
        "color while it's still tacky can trap solvent and wrinkle, cloud or streak the finish. "
        "PRIMER and CLEAR are different: their next coat can be applied once the previous one "
        "flashes to tacky. Before wet-sanding and buffing, let the clear cure completely — that "
        "takes longer, and rushing the buff is a common mistake. Drying speed changes with "
        "temperature, humidity and how heavy you sprayed, so always confirm your product's "
        "tech-sheet times.",
    ),
    (
        "When can I apply the second coat of basecoat, pearl or candy?",
        "Wait until the first coat is COMPLETELY dry — not just tacky. Basecoats, pearl "
        "basecoats and candies must be fully dry before the next coat, or you risk trapping "
        "solvent, streaking or clouding the color. This is different from primer and clear "
        "coat, where the next coat can go on once the previous one is tacky. Temperature, "
        "humidity and how heavy you sprayed all affect dry time, so give color coats the time "
        "they need before recoating.",
    ),
    (
        "Can I mix two colors together to make a custom color?",
        "Solid basecoats from the same line can generally be intermixed to create your own "
        "color; candies tint rather than blend like solids. Whatever you mix, write down your "
        "ratios so you can repeat the exact color later.",
    ),
    (
        "Do I need a respirator and safety gear to spray?",
        "Yes — always. Spray in a well-ventilated space with a proper paint respirator (not a "
        "dust mask), plus eye protection and gloves. Automotive paints and clears contain "
        "solvents, so protect yourself and keep sources of flame/spark away.",
    ),
    (
        "Can I use the paint on fiberglass or carbon fiber parts?",
        "Yes, with prep: sand, fill any pinholes, apply primer/sealer, then base and clear. It "
        "works great on body kits, panels and other composite parts as long as the surface is "
        "smooth and sealed first.",
    ),
    (
        "How much paint do I need for my project?",
        "It depends on the size of the part, the color, and how many coats — a small part like a "
        "helmet or a set of calipers needs far less than a full car. Tell us what you're "
        "painting and roughly its size and we'll help you pick the right size and quantity.",
    ),
    (
        "Can I use the paint on chrome or bare metal?",
        "Bare steel and aluminum need to be cleaned, etched/primed and sealed before color so it "
        "adheres and won't flash rust. Chrome is very slick — scuff it well and use an adhesion "
        "promoter or the color can lift. Proper prep is everything on metal.",
    ),
]


async def _existing_hashes(qs: list[str]) -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT question, content_hash FROM faqs WHERE question = ANY(:qs)"),
                {"qs": qs},
            )
        ).all()
        return {r[0]: r[1] for r in rows}


async def run_seed() -> None:
    """Inserta el conocimiento que falte en `faqs` y ACTUALIZA las entradas cuya
    respuesta cambió (comparando content_hash). Idempotente y barato: solo embebe
    lo nuevo o lo editado."""
    questions = [q for q, _ in KNOWLEDGE]
    try:
        have = await _existing_hashes(questions)
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo consultar faqs; se omite seed de conocimiento")
        return
    missing = [
        (q, a)
        for q, a in KNOWLEDGE
        if have.get(q) != embeddings.content_hash(q + "|" + a)
    ]
    if not missing:
        _log.info("Base de conocimiento al día (%s entradas); nada que sembrar", len(KNOWLEDGE))
        return
    _log.info("Sembrando %s entradas de conocimiento nuevas...", len(missing))
    try:
        vectors = await embeddings.embed_batch([q for q, _ in missing])
        async with AsyncSessionLocal() as session:
            for (q, a), vec in zip(missing, vectors):
                await kb_store.upsert_faq(
                    session,
                    question=q,
                    answer=a,
                    synonyms=[],
                    embedding=vec,
                    recommended_skus=[],
                    related_product_id=None,
                    post_action="offer_assistance",
                    time_used=0,
                    content_hash=embeddings.content_hash(q + "|" + a),
                )
        _log.info("Sembradas %s entradas de conocimiento", len(missing))
    except Exception:  # noqa: BLE001
        _log.exception("Falló el seed de la base de conocimiento")
