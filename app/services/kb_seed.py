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
        "It depends on what you're spraying. COLOR coats — Tropical Glitz basecoats and candy "
        "basecoats — need about 15–25 minutes of flash time, and each coat must be dry with an "
        "even, uniform appearance and NO wet spots before the next one goes on; recoating color "
        "while it's still wet or tacky can trap solvent and wrinkle, cloud or streak the finish. "
        "PRIMER, SEALER and CLEAR are different: their next coat goes on once the previous one "
        "flashes to a slightly tacky stage — don't apply the basecoat number to them, use that "
        "product's tech sheet. Before wet-sanding and buffing, let the clear cure completely — "
        "generally at least 24 hours before handling or polishing and about 5–7 days for a full "
        "cure, depending on the clear. Cool temperatures, high humidity, poor airflow and heavy "
        "coats all stretch these times, so always confirm your product's technical data sheet.",
    ),
    (
        "When can I apply the second coat of basecoat, pearl or candy?",
        "For Tropical Glitz basecoats and candy basecoats, allow roughly 15–25 minutes of flash "
        "time between coats. Don't go by the clock alone — the surface should look even and "
        "uniform with no wet spots before you recoat. Flash time is the wait BETWEEN coats; it's "
        "not the same as drying or full curing time. Cool temperatures, high humidity, limited "
        "airflow or heavy coats will make it take longer. This 15–25 minute guideline is for "
        "basecoats and candy basecoats only — primers, sealers, intercoats and clears have their "
        "own recoat windows in their technical data sheets. Rather than touching the paint to "
        "test it, check a masked edge or a test panel.",
    ),
    (
        "What is the difference between the Luscious, Eclipse and Cosmic lines?",
        "All three are candy basecoats — they give you candy depth but spray like a basecoat. "
        "LUSCIOUS uses medium-coarse metallics: candy depth in an easy, basecoat-style "
        "application. ECLIPSE uses high-coarse metallics and is formulated so dark that at rest "
        "it reads like a regular black basecoat — until the light hits it and the candy vibrance "
        "comes alive. COSMIC uses medium-coarse metallics PLUS mini prismatic metal flakes, so on "
        "top of the normal metallic reflection you get extra sparkle and flashes of color in the "
        "light. Tell us the look you're after and we'll point you to the right series.",
    ),
    (
        "What is the Eclipse series?",
        "Eclipse is our candy basecoat series built around high-coarse metallics and a very dark "
        "tone — sitting still it looks like a straight black basecoat, but when light hits it the "
        "candy vibrance shows through. It's the pick when you want a color that hides in the "
        "shade and comes alive in the sun. It sprays like a basecoat while giving you candy depth.",
    ),
    (
        "What is the Cosmic series?",
        "Cosmic is our candy basecoat series with medium-coarse metallics plus mini prismatic "
        "metal flakes mixed in. Beyond the normal metallic reflection, those prismatic flakes "
        "throw extra sparkle and flashes of color as the light moves across the paint. Like our "
        "other candy basecoats, it has candy depth but sprays like a basecoat.",
    ),
    (
        "What is the Luscious series?",
        "Luscious is our candy basecoat series with medium-coarse metallics. You get true candy "
        "depth, but it sprays like a regular basecoat, which makes it a great choice when you "
        "want the candy look without a separate candy-over-base process.",
    ),
    (
        "Can you match a custom color, or a Cerakote, PPG or House of Kolor color?",
        "Custom color-match requests have to be reviewed by our customer service team first — we "
        "can't confirm a match sight-unseen or from a photo, since screens and lighting shift how "
        "a color reads. Email support@tropicalglitz.net with the manufacturer's name, the color "
        "name or code, reference photos, and a physical sample or color chip if you have one, "
        "plus the type of paint and the quantity you need. The team will review it and let you "
        "know whether we can produce that color. Keep in mind the ground coat, lighting, "
        "application technique and number of coats all affect how the final color looks.",
    ),
    (
        "How long before I can apply clear coat over my basecoat?",
        "For Tropical Glitz candy and standard basecoats, wait about 30 minutes after your FINAL "
        "coat before clearing. For paints containing metal flakes, give it about 1 to 1½ hours — "
        "the flake particles slow solvent evaporation. Either way, don't go by time alone: the "
        "basecoat has to be completely dry and evenly flashed with no wet spots. Cool "
        "temperatures, high humidity, limited airflow and heavy coats all add time. If you used "
        "an activator, additive or reducer that can change the drying time too. Don't touch the "
        "surface to test it — check a masked edge or test panel — and don't force-dry with heat "
        "unless that product's technical data sheet allows it.",
    ),
    (
        "How long does the paint need to cure?",
        "Your basecoat does NOT need to fully cure before clear — it only needs to be completely "
        "dry and properly flashed. The cure time for the finished job comes mainly from the clear "
        "coat you used. As a general guideline, give it at least 24 hours before handling or "
        "polishing and about 5–7 days for a full cure. Those are general estimates, not "
        "guarantees: temperature, humidity, airflow and how many/how heavy your coats were all "
        "change it. 'Dry to the touch', 'ready to handle', 'ready to polish' and 'fully cured' "
        "are four different stages. Tell us which clear you used and what you want to do next "
        "(handle, assemble, sand, polish, wax, ceramic coat) and we'll point you to that clear's "
        "technical data sheet — for SPI clears, use the SPI tech sheets.",
    ),
    (
        "Can I sand between coats?",
        "Under normal conditions, no — you shouldn't sand between coats of basecoat. Let each "
        "coat flash properly and lay the next one on. If you have to fix dust, a run or another "
        "flaw, let the paint dry completely first, sand just that spot lightly, clean the surface "
        "well, and then reapply basecoat so the finish stays uniform. Be extra careful with "
        "candy, pearl, metallic and flake finishes: sanding disturbs the color, the metallic "
        "orientation and the flake pattern, so after a repair you'll usually need another full, "
        "even coat over the WHOLE area to get the color and particle layout back. Primers can be "
        "sanded once they've dried the recommended time, and clear can be sanded to fix "
        "imperfections or when you're adding more clear outside its recoat window. Always follow "
        "the technical data sheet for the exact product.",
    ),
    (
        "What happens if I wait too long before recoating?",
        "If too much time passes, the previous layer can fall outside its recoat window, and the "
        "next coat may not bond properly — that's what leads to peeling, chipping, lifting, "
        "wrinkling or delamination. Once the maximum recoat time is exceeded, the surface usually "
        "has to be cleaned and scuffed or sanded to create a mechanical bond before you continue. "
        "Waiting also gives dust, oil and moisture time to settle on the surface. Recoating too "
        "SOON is its own problem though, because it traps solvent. Every primer, basecoat, "
        "intercoat and clear has a different recoat window, so follow that product's technical "
        "data sheet and account for temperature, humidity, airflow and coat thickness. If the "
        "finish is already lifting, wrinkling, peeling or delaminating, stop spraying and contact "
        "us at support@tropicalglitz.net before you add anything else.",
    ),
    (
        "What spray gun tip size should I use?",
        "Tip size depends on what you're spraying and, for flake, how big the flake is. "
        "Basecoats, pearls, intercoat clear and clear coats: 1.3–1.4mm. Metal flakes mixed into "
        "intercoat clear go up from there — .004 inch flake: 1.4–1.6mm; .008 inch flake: "
        "1.7–2.0mm; .015 inch flake: 2.0–2.5mm; .025 inch flake: 2.5mm. Anything larger than .025 "
        "inch should go through a Flake Slinger dry-flake gun instead. The rule of thumb is "
        "simple: bigger flake needs a bigger tip to pass through cleanly. These are general "
        "recommendations — your gun, your mix and your technique can shift the ideal setup, so "
        "check the product's technical data sheet too.",
    ),
    (
        "What PSI or air pressure do you recommend?",
        "Start around 20–30 PSI for most of our products, then dial it in. As starting points: "
        "basecoat 20–30 PSI, clear coat 25–30 PSI, intercoat clear 20–30 PSI, pearls 20–25 PSI, "
        ".004–.008 inch metal flake in intercoat clear 25–30 PSI, and .015 inch or larger flake "
        "start around 30 PSI and go up from there if needed. Measure the pressure AT the gun with "
        "the trigger fully pulled and air flowing — that reading is the one that matters. Test "
        "your pattern on a sample panel and adjust gradually until the product atomizes evenly. "
        "These are starting ranges, not fixed settings: your exact pressure depends on your gun, "
        "tip size, reducer, temperature and the product, so also follow your spray gun "
        "manufacturer's pressure limits and the product's technical data sheet.",
    ),
    (
        "Do you ship paint to Hawaii, Alaska or Puerto Rico?",
        "Yes - we ship all of our products to Hawaii, Alaska, Puerto Rico and other US states and "
        "territories outside the mainland, and that INCLUDES flammable paint. There's no product "
        "restriction; the only difference is transit time. Mainland US orders usually arrive in "
        "2-5 business days, while non-mainland destinations take about 10-16 business days from "
        "the ship date, so order a little earlier if you're working to a deadline. The "
        "dry-goods-only rule applies to international orders outside the US, Canada and Mexico - "
        "not to Hawaii or Alaska.",
    ),
    (
        "Where do you ship to?",
        "We ship across the US and internationally. Within the US we ship everywhere - all 50 "
        "states plus Puerto Rico and other territories - and every product, including flammable "
        "paint; Hawaii, Alaska and other non-mainland destinations just take 10-16 business days "
        "instead of the usual 2-5. Internationally we ship worldwide, but outside the US, Canada "
        "and Mexico we can only send dry goods (metal flakes, pearls and leaf), because flammable "
        "liquid paint can't go overseas. International rates run about $30-$100 depending on the "
        "country, with 10-16 business days to Canada, Australia and the UK and 15-25 business "
        "days elsewhere. US orders over $499 ship free (can't be combined with a discount code).",
    ),
    (
        "Can you make paint by OEM or factory paint code?",
        "Yes! Our Paint by Code service (https://tropicalglitz.net/products/paint-by-code) "
        "mixes custom paint matched to your vehicle's original equipment manufacturer (OEM) "
        "paint code — for cars and motorcycles. To order, provide the paint code, the vehicle "
        "make (brand), model and year so we can formulate the exact color. It comes in Ready "
        "to Spray 2oz and 4oz, 12oz Spray Can, Pint, Quart and Gallon.",
    ),
    (
        "Do you match factory colors for my car or motorcycle?",
        "Yes — factory color matching is available through Paint by Code "
        "(https://tropicalglitz.net/products/paint-by-code). Just have your OEM paint code, "
        "vehicle make, model and year ready when you order, and we'll formulate the color to "
        "match. The code is usually on a sticker in the door jamb, glove box or under the "
        "hood — if you're not sure where to find it, tell us your vehicle and we'll help.",
    ),
    (
        "Do your paint colors come in spray cans?",
        "Yes — nearly every paint we sell (candies, candy basecoats, pearls, metallic and "
        "flake basecoats) is also available as a ready-to-spray 12oz Spray Can variant on its "
        "product page. Separately, the Drip® series is our Pantone-matched solid-color line "
        "that comes exclusively in spray cans. Tell us the color and finish you're after — "
        "candy, pearl, flake or solid — and we'll point you to it in spray can form.",
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
