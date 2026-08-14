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
        "the reduction ratio printed for that product — don't change the ratio to compensate for "
        "the weather. Temperature is handled by picking the right REDUCER SPEED instead: fast for "
        "60-70F, medium for 65-80F, slow for 75-90F and very slow at 95F and above, going by the "
        "temperature in the spray area rather than outside.",
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
        "Yes — many of our products spray through an airbrush, but the needle size, the reduction "
        "and the pressure all depend on which product you're spraying. Follow that product's own "
        "mixing ratio; don't over-reduce it just to squeeze it through a smaller nozzle. Anything "
        "with particles in it — pearls, metallics, flake — needs a larger needle/nozzle than a plain "
        "basecoat, and larger metal flakes may not pass through an airbrush at all. Tell us the "
        "product, whether it's ready to spray, your airbrush model and needle size (and the flake "
        "size if there's flake) and we'll dial it in with you. Always test on a sample panel first, "
        "and wear proper respiratory protection.",
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
        "Suspend the flake in intercoat clear and spray it in light, even coats over your base, then "
        "bury it with clear and level it out. Mix the carrier first — Tropical Glitz Intercoat Clear "
        "and Reducer 1:1 — then add the flake gradually; that 1:1 stays the same no matter how much "
        "flake goes in. The amount of flake controls how heavy the sparkle looks, so start light, "
        "shoot a sample panel and build up. Overloading the mix clogs the gun, lands unevenly and "
        "leaves a rough surface that's hard to bury. Keep it agitated while you spray — flake "
        "settles fast.",
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
        "days elsewhere. US orders over $499 ship free, and that stacks with our WELCOME code.",
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
    (
        "Can I spray Tropical Glitz products through an airbrush?",
        "Yes, many of them airbrush well — but there's no single needle size, reduction or pressure "
        "that works for everything. It depends on the product. Follow that product's listed mixing "
        "ratio and don't over-reduce it just to get it through a smaller nozzle. Products with "
        "pearl, metallic or any other particle need a larger needle/nozzle than a plain basecoat, "
        "and metal flake needs special thought — bigger flakes may not pass through an airbrush at "
        "all, and nozzle size alone doesn't guarantee they will, because flake shape, the mix and "
        "the airbrush's internal passages all play a part. If the particles are too big, a properly "
        "sized spray gun or an El Flake Slinger dry-flake gun is the way to go. Test your mix and "
        "pattern on a sample panel before the real project, and always use a respirator and good "
        "ventilation. Tell us the product, whether it's ready to spray, your airbrush model and "
        "needle size, and the flake size if there's flake, and we'll help you set it up.",
    ),
    (
        "When do I need to use a dry flake gun?",
        "Not for every flake. A dry flake gun earns its keep with larger flakes — .025 inch and up — "
        "or when you want a heavy flake load. Fine and many medium flakes mix into intercoat clear "
        "just fine and spray through a conventional gun with the right tip size. With the big ones a "
        "dry gun makes application easier and cuts down on clogging. It lays the flake down on its "
        "own instead of carrying it in intercoat, and either way the flake still has to be buried "
        "under clear to end up smooth and protected. Tell us your flake size and whether you're "
        "after a light even sparkle or heavy coverage and we'll point you to the right setup — and "
        "test it on a sample panel before the project.",
    ),
    (
        "Which flake gun do you recommend?",
        "It depends on whether you're spraying the flake wet or dry. WET, mixed into intercoat "
        "clear: the Tropical Glitz HVLP Flake Gun, which comes with a 2.0mm or a 2.5mm tip. Go 2.0mm "
        "for smaller flake and lighter work, 2.5mm for larger flake or heavy coverage — the bigger "
        "opening lets the flake through with less chance of clogging. DRY: the Tropical Glitz El "
        "Flake Slinger Dry Flake Gun, built specifically to spray flake dry and give you control "
        "over coverage and distribution. As a guide for flake mixed into intercoat clear: .004 inch "
        "flake 1.4-1.6mm, .008 inch 1.7-2.0mm, .015 inch 2.0-2.5mm, .025 inch 2.5mm, and anything "
        "larger than .025 inch should go through the El Flake Slinger. Don't use the El Flake "
        "Slinger for flake mixed into intercoat, and don't use the HVLP Flake Gun for loose dry "
        "flake. Tip size alone doesn't guarantee a given flake will pass — flake shape, the mix, the "
        "reduction and the gun's internal passages matter too, so test on a sample panel first.",
    ),
    (
        "How much flake should I add into the intercoat clear?",
        "There's no single amount that works for every project — it depends on the flake size and "
        "whether you want a light, medium or heavy finish. Start with the carrier: mix Tropical "
        "Glitz Intercoat Clear and Tropical Glitz Reducer 1:1. Once that's mixed, add the flake "
        "gradually and stir it in thoroughly. Begin with a small amount, spray a sample panel, and "
        "add more if you want more coverage. Don't overload it: too much flake makes it hard to "
        "spray, causes clogging and uneven distribution, and leaves a rough finish that takes far "
        "more clear to bury. For heavy coverage you're better off with several controlled coats, or "
        "with an El Flake Slinger dry-flake gun, than with an overloaded mix. Keep the mixture "
        "agitated while you spray because flake settles quickly, and write down how much you used so "
        "your next batch matches. Note that 1:1 is the Intercoat-to-Reducer ratio, not a flake "
        "ratio — it stays 1:1 no matter how much flake you add.",
    ),
    (
        "How do I mix Tropical Glitz candy concentrates?",
        "It's a two-stage mix. First combine 8 parts Tropical Glitz Intercoat Clear with 1 part "
        "Candy Concentrate and mix thoroughly. Then reduce that whole mixture 1:1 with Tropical "
        "Glitz Reducer — an amount of reducer equal to the intercoat and concentrate combined. The "
        "order matters: Intercoat Clear, then Candy Concentrate, mix, then Reducer, mix again. "
        "Example for 18 oz of sprayable candy: 8 oz Intercoat Clear + 1 oz Candy Concentrate = 9 oz "
        "of candy mixture, then add 9 oz of Reducer for roughly 18 oz sprayable. Scaling up: 36 oz "
        "final = 16 + 2 + 18; 72 oz = 32 + 4 + 36; 144 oz = 64 + 8 + 72. An easy way to think about "
        "it is that the final sprayable amount splits into 18 parts — 8 intercoat, 1 concentrate, 9 "
        "reducer. The 8:1 applies only before reduction; don't treat the concentrate as one part of "
        "the finished mix, and keep every measurement in the same unit. Candy Concentrate is very "
        "transparent, so the ground coat underneath drives the final color — a compatible metallic "
        "or pearl ground coat is usually the right call, and the color builds through multiple even "
        "coats rather than by adding extra concentrate. Always shoot a sample panel first.",
    ),
    (
        "How do I choose a reducer?",
        "Pick the reducer by the temperature of your spray area and the surface you're painting, not "
        "the temperature outside. Fast Reducer 60-70F, Medium Reducer 65-80F, Slow Reducer 75-90F, "
        "Very Slow Reducer 95F and above. Faster reducers evaporate quicker for cooler conditions; "
        "slower ones stay wet longer so the paint has time to flow and level when it's hot. Project "
        "size, airflow and conditions matter too — if you're right on the line between two ranges, a "
        "big job that takes longer to spray often does better on the slower one. Use the reducer and "
        "the mixing ratio specified for your product, and don't change the ratio to compensate for "
        "temperature. Products labeled Ready to Spray don't take reducer at all. Reducer speed is a "
        "different thing from activator speed — don't swap one for the other. If you're spraying "
        "below 65F, or you're not sure which one you need, check the product's technical data sheet "
        "or reach out to us before you spray.",
    ),
    (
        "How do I achieve a bass boat-like finish?",
        "That look comes from dense metal-flake coverage over a compatible ground coat, buried under "
        "enough clear to end up smooth and glossy. Prep and prime with products compatible with your "
        "substrate, then lay an even ground coat in a color that complements the flake — a similar "
        "tone helps coverage and keeps thin spots from showing. Apply the flake until you've got the "
        "coverage you want: for wet application mix Intercoat Clear and Reducer 1:1, add the flake "
        "gradually, and spray several controlled coats while keeping the mix agitated. For heavier "
        "coverage or flakes .025 inch and up, an El Flake Slinger dry-flake gun makes it much "
        "easier. Let the flake layer flash properly, and if the products allow it, a flake-free coat "
        "of intercoat helps lock everything down. Then clear per that clear coat's technical data "
        "sheet — heavy flake often needs more than one clear session. Once it's cured, level-sand "
        "the clear carefully without cutting into the flakes, add more clear if the surface is still "
        "textured, and let the final clear cure before the last sanding and polish. Shoot a sample "
        "panel first to confirm the ground color, flake coverage and overall look. If this is going "
        "on an actual boat rather than a vehicle, make sure every primer, carrier and clear is "
        "approved for fiberglass or gelcoat and for that kind of marine exposure — an automotive "
        "clear shouldn't be assumed safe for continuous water immersion.",
    ),
    (
        "How do I bury heavy flakes?",
        "Heavy flake normally takes several layers of clear to come out level. After the flake goes "
        "on, let the carrier or intercoat flash properly; if the products allow it, a flake-free coat "
        "of intercoat clear helps lock the flakes in place before you clear. Start the clear per its "
        "technical data sheet — a light first coat helps keep the flakes from moving, followed by the "
        "recommended wet coats and flash times. Don't try to bury it all at once with excessively "
        "thick coats or too many coats in one session; that traps solvent and causes problems. Once "
        "the first clear session has cured for the required time, sand the clear until it's level, "
        "being careful not to cut through into the flakes — sanding into them dulls their color and "
        "reflection. If the surface is still textured, prep it per the clear's instructions and "
        "apply another round, repeating until the flakes are fully covered and the surface is "
        "smooth. Let the final clear cure before the last sanding and polishing. If you see lifting, "
        "wrinkling, solvent pop, peeling or delamination, stop and contact us. And if you're using an "
        "SPI clear, follow that clear's SPI technical data sheet for its coats, flash and cure times.",
    ),
    # --- Correcciones de entradas heredadas del sistema anterior (REP) ---
    # Se repiten las preguntas EXACTAS para que el upsert reemplace la respuesta
    # vieja: decían .040 para el flake gun seco y descartaban el flake en
    # aerógrafo de plano, y eso ya no coincide con la guía aprobada.
    (
        "Flake bigger than .004 → dry flake gun?",
        "No — bigger than .004 doesn't automatically call for a dry flake gun. Fine and many "
        "medium flakes mix into intercoat clear and spray through a conventional gun with the "
        "right tip size. A dry flake gun starts to make sense at .025 inch, and for flake larger "
        "than .025 inch use an El Flake Slinger dry-flake gun. It's also the easier route any "
        "time you want really heavy coverage, whatever the size.",
    ),
    (
        "Biggest flake for 2.0 flake gun?",
        "A 2.0mm tip handles .004, .008 and .015 flake mixed into intercoat clear. At .015 you're "
        "at the top of its range — 2.0 to 2.5mm both work there, and the 2.5mm passes it more "
        "easily with less chance of clogging. For .025 inch go to the 2.5mm tip, and above .025 "
        "inch move to an El Flake Slinger dry-flake gun. Tip size alone isn't a guarantee: flake "
        "shape, how heavy you load the mix and the gun's internal passages all matter, so test on "
        "a sample panel first.",
    ),
    (
        "Metal flakes in airbrush?",
        "It depends on the flake size — this isn't a flat yes or no. Larger flakes generally won't "
        "pass through a standard airbrush and will clog the nozzle, so the needle/nozzle has to be "
        "big enough for the particle size you're spraying. Even then, nozzle size alone doesn't "
        "guarantee it: flake shape, the mixture and the airbrush's internal passages all affect "
        "flow. Tell us your flake size and airbrush model and we'll tell you whether it will work. "
        "If the flake is too big, use a properly sized spray gun, or an El Flake Slinger dry-flake "
        "gun for dry application. Either way, test it on a sample panel before the real project.",
    ),
    (
        "What are the available flake sizes of metal flake?",
        "Our metal flake comes in .004, .006, .008, .015, .025 and .040 — but not every color is "
        "offered in every size. .008, .015 and .004 are available across most of the line, while "
        ".006, .025 and .040 are offered on a smaller selection of colors. The product page for "
        "the color you want is the place to check which sizes it actually comes in. Tell us the "
        "color you're after and we'll tell you what's available in it.",
    ),
    (
        "What metal flake sizes do you offer, and does every color come in every size?",
        "We offer .004, .006, .008, .015, .025 and .040 metal flake, and no — availability depends "
        "on the color. Most colors come in .008, .015 and .004; the .006, .025 and .040 sizes are "
        "offered on a limited selection of colors. Always check the product page for the specific "
        "color to see which sizes it comes in. Size also changes how you spray it: the smaller "
        "sizes mix into intercoat clear and go through a conventional gun with the right tip, while "
        "anything larger than .025 should go through an El Flake Slinger dry-flake gun.",
    ),
    (
        "I subscribed to your email but never got my 10% off code",
        "Sorry about that — no need to wait on it. The welcome code is WELCOME and it takes 10% "
        "off your order. Just enter it at checkout. There's no minimum purchase, and it stacks "
        "with our free shipping on US orders over $499. One thing to know: it's one use per "
        "customer, so it's meant for your first order with us. If it doesn't apply to something "
        "in your cart, that item is outside the offer — the cart total will show you exactly what "
        "it took off.",
    ),
    (
        "Do you have a discount code or coupon?",
        "If this is your first order with us, yes — use WELCOME at checkout for 10% off. No "
        "minimum purchase, and it stacks with free shipping on US orders over $499. It's one use "
        "per customer, so it's for first-time buyers. Beyond that we don't have other codes "
        "running unless you see one advertised on the site.",
    ),
    (
        "I signed up for SMS but didn't receive the welcome discount",
        "No problem — the code is WELCOME, for 10% off your order, entered at checkout. No "
        "minimum purchase and it works alongside free shipping on US orders over $499. It's one "
        "use per customer so it's intended for a first order. If it doesn't come off in the cart, "
        "let us know at support@tropicalglitz.net and we'll sort it out.",
    ),
    (
        "Is the WELCOME code for everyone or only new customers?",
        "WELCOME is one use per customer, so in practice it's the first-time buyer offer — 10% "
        "off, no minimum, and it stacks with free shipping on US orders over $499. If you've "
        "already used it on a previous order it won't go through a second time. If you're a "
        "returning customer looking for a deal, email support@tropicalglitz.net and the team can "
        "let you know what's currently running.",
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
