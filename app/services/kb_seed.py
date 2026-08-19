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
        "Yes — as long as the caliper's maximum surface temperature stays within the heat "
        "rating of the COMPLETE coating system. Many of our colors list heat resistance up to "
        "400°F, but confirm that on the product page for the exact color; not every product or "
        "clear carries the same rating, and the whole system is limited by whichever product "
        "has the lowest one. That means the primer, basecoat, intercoat and clear all have to "
        "suit the temperatures the caliper will see. For normal street driving that's usually "
        "fine. For racing, track days, repeated hard braking or heavy towing, temperatures can "
        "go past what the system handles — that calls for a dedicated high-temperature "
        "brake-caliper coating instead. Prep matters: calipers must be cool and free of brake "
        "dust, rust, grease, oil, silicone and brake fluid. Never paint the pads, rotors, "
        "piston surfaces, rubber seals or boots, bleeder screws, hose connections, slide pins, "
        "threads or mounting surfaces. Brakes are a safety system, so removal, masking and "
        "reinstallation should be done by someone qualified to work on brakes, and the whole "
        "system has to cure fully before the calipers go back on. Tell us your color and clear "
        "and how you drive the car, or call 786-383-3013, and we'll confirm it for your setup.",
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
        "Yes. Clear Lexan/polycarbonate bodies get painted from the INSIDE, so the Lexan itself "
        "stays as the glossy outer surface and protects the paint. The key is Tropical Glitz "
        "The Hornet Adhesion Promoter — it works on Lexan and goes down first over the clean "
        "interior, creating a flexible bond so the color moves with the body instead of "
        "cracking. Wash the inside with warm water and mild dish soap to strip the "
        "mold-release agents, let it dry, and do NOT sand or scuff clear Lexan — those "
        "scratches show through from the outside. Then spray in multiple light coats, and "
        "remember you're painting in reverse: the first color you spray is what shows from "
        "the outside, with the ground coat going on behind it. For hard-plastic RC parts "
        "(bumpers, wings, chassis) you scuff and use an adhesion promoter as you would on any "
        "plastic part. Test piece first — the ground coat changes the look a lot.",
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
        "Yes, as long as the existing finish is fully cured, firmly attached and compatible. "
        "Scuff it to a uniform dull finish, clean and degrease thoroughly, and on very slick "
        "surfaces use an adhesion promoter, then seal/base and clear as normal. Skipping the "
        "scuff and degrease is the #1 cause of peeling. What you should NOT do is paint over a "
        "finish that's peeling, cracking, lifting, bubbling, rusting or delaminating — that has "
        "to be removed or repaired first, because whatever you spray on top fails with it. If "
        "bare metal, plastic or fiberglass ends up exposed, it needs the right primer, sealer or "
        "adhesion promoter. If you don't know what the existing paint is, do a test area first: "
        "a compatible sealer may be needed to isolate it and avoid lifting, wrinkling or "
        "bleeding. And remember candy, pearl and chameleon colors are semi-transparent, so the "
        "old color and how uniform it is will change the final look.",
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
        "What is the difference between the Luscious, Eclipse, Cosmic, Orbit Shift, Lollipop "
        "and Seductive lines?",
        "They're all candy basecoats — candy depth, but they spray like a basecoat. LUSCIOUS: "
        "medium-coarse metallics, candy depth in an easy basecoat-style application. ECLIPSE: "
        "high-coarse metallics, formulated so dark that at rest it reads like a regular black "
        "basecoat, until the light hits and the candy vibrance comes alive. COSMIC: "
        "medium-coarse metallics PLUS mini prismatic metal flakes, so on top of the normal "
        "metallic reflection you get extra sparkle and flashes of color. ORBIT SHIFT: a "
        "chameleon basecoat with a settled tone — at first glance it looks like a deep, rich "
        "candy, but as the angle changes it shifts hard like a chameleon while keeping that "
        "candy depth; it must go over a BLACK ground coat to shift properly. LOLLIPOP: very "
        "fine metallics for a bright, vibrant color with a smooth, silky finish. SEDUCTIVE: a "
        "true candy with medium-coarse metallics — the depth and intensity of a traditional "
        "candy without the complexity of a traditional candy application. Tell us the look "
        "you're after and we'll point you to the right series.",
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
    (
        "Can I apply Tropical Glitz paint over existing paint?",
        "Generally yes, if the existing finish is fully cured, firmly attached, compatible and "
        "properly prepped: clean, dry, free of wax, grease, silicone and dirt, and sanded or "
        "scuffed to kill the gloss so the new paint can grab. Never spray over a finish that's "
        "peeling, cracking, lifting, bubbling, rusting or delaminating — remove or repair those "
        "areas first. If any metal, plastic or fiberglass is exposed, it needs the right primer, "
        "sealer or adhesion promoter. If the existing paint is fresh, check its recoat window "
        "before adding anything. If you don't know what it is, prepare a test area first; a "
        "compatible sealer may be needed to isolate an unknown or sensitive finish and reduce the "
        "risk of lifting, wrinkling or bleeding. Keep in mind candy, pearl, chameleon and other "
        "semi-transparent colors are affected by what's underneath, so the color and uniformity of "
        "the old finish will change the final appearance. Follow the tech sheets for every product "
        "in the system, and do a test panel before the whole project.",
    ),
    (
        "Can I activate your basecoat?",
        "Yes — pourable Tropical Glitz basecoats can be activated with Tropical Glitz Basecoat "
        "Activator, which helps adhesion and durability. Two things people get wrong: Basecoat "
        "Activator and Intercoat Activator are different products with different mixing ratios, so "
        "don't substitute one for the other, and the activator does NOT replace reducer — the "
        "basecoat still gets reduced as directed for that product. Use the amount and procedure on "
        "the current product label. Don't add activator to an aerosol can, and don't use a "
        "clear-coat or another brand's activator unless we've confirmed it's compatible. Activated "
        "material has a limited usable time, so mix only what you need. If you don't have the "
        "instructions in front of you, call our technical team at 786-383-3013 or email "
        "support@tropicalglitz.net before you mix — we'd rather give you the exact ratio than have "
        "you guess at it.",
    ),
    (
        "Can I mix Tropical Glitz pearls and flakes into another paint system?",
        "Often yes, but compatibility has to be confirmed first — we can't guarantee how another "
        "manufacturer's product will behave with ours. Pearls and flakes need a compatible carrier, "
        "like an intercoat clear or another binder the paint-system manufacturer approves. For the "
        "most predictable results we'd use Tropical Glitz Intercoat Clear. If you're using another "
        "brand's carrier, check its technical data sheet or ask that manufacturer whether it "
        "accepts dry pearls or metal flakes. Never mix pearls or flakes directly into reducer, "
        "activator or hardener. Mixing brands can affect performance and warranties, so a test "
        "panel is strongly recommended. Tell us which pearl or flake you have (and the flake size), "
        "plus the brand and type of the other system and whether it's solvent-based or waterborne, "
        "and we'll help — or call 786-383-3013 / email support@tropicalglitz.net.",
    ),
    (
        "Why is my paint cloudy or hazy?",
        "First thing to rule out: basecoat normally dries DULL or matte before clear goes on — dull "
        "isn't the same as cloudy or milky. If it's truly hazy, common causes are high humidity or "
        "moisture in the air line, coats applied too heavy, not enough flash time, trapped "
        "solvents, the wrong reducer or mixing ratio, surface contamination, dry spray, or gun "
        "settings. On metallic, pearl, candy or flake it can also come from uneven passes, "
        "inconsistent overlap, gun distance, poor particle orientation, or too much pearl or flake "
        "in the mix. If the CLEAR looks milky, stop adding coats and let it dry under proper "
        "temperature, humidity and airflow. Don't clear over a cloudy basecoat and don't add more "
        "material to hide it — that traps the problem. If it's still there once everything is dry, "
        "that layer likely has to be corrected and reapplied. Tell us which product it is and when "
        "the haze appeared, or call 786-383-3013 / email support@tropicalglitz.net.",
    ),
    (
        "Why is my paint peeling or delaminating?",
        "Peeling means a layer didn't bond — either to the surface or to the layer under it. The "
        "usual causes are inadequate sanding or prep, contamination (wax, grease, silicone, dust, "
        "moisture), painting over an unstable existing finish, incompatible products, going outside "
        "a recoat window, coats too heavy or without enough flash, trapped solvents, a wrong ratio "
        "or reducer, a missing primer/sealer/adhesion promoter, or excessive film thickness. Where "
        "it separates is a clue: clear letting go of the basecoat points to a recoat-window, "
        "contamination or compatibility issue, while the whole system lifting off the substrate "
        "points to prep or a missing primer. Do NOT paint or clear over material that's already "
        "peeling. It has to come off back to a firmly attached layer, and the cause has to be found "
        "before you refinish. Email photos of the affected area and the underside of the peeled "
        "coating to support@tropicalglitz.net, or call 786-383-3013, and we'll help you work out "
        "what happened.",
    ),
    (
        "Why isn't my paint covering?",
        "Start with what the product is. Candies, pearls, chameleons and other effect colors are "
        "designed to be transparent or semi-transparent — they aren't meant to hide what's "
        "underneath, and their final look depends heavily on the color and uniformity of the ground "
        "coat. Pearls and flakes create an effect, not coverage. If it's an opaque basecoat that "
        "isn't covering, the usual causes are the wrong or uneven ground-coat color, not mixing "
        "thoroughly, over-reduction, coats applied too light or too dry, gun settings, inconsistent "
        "overlap, or simply not enough coats. Mix the material well to redistribute settled "
        "pigment, then apply even coats with consistent distance, speed and overlap and the "
        "recommended flash between them. Don't lay on heavy coats to cover faster — that brings "
        "runs, uneven color, trapped solvents and long dry times. With candy, keep the coat count "
        "the same across every panel, because each coat deepens the color. A test panel is the way "
        "to confirm the ground coat and coat count before the real job.",
    ),
    (
        "Why is my spray can clogging?",
        "Usually paint, pearl or flake has settled in the can or dried in the tip. Bring the can to "
        "normal room temperature first, then shake it thoroughly for the time stated on the label — "
        "and with pearl or flake keep shaking during application to keep the particles suspended. "
        "Hold it upright, press the tip all the way down, and use steady passes. If the tip clogs, "
        "stop; if that plastic tip is the removable type, take it off and clean just the tip with "
        "the cleaner recommended for that product, then let it dry before putting it back. When "
        "you're done spraying, clear the tip the way the can instructs — if it says to, turn it "
        "upside down and spray briefly until only clear propellant comes out. Some hard safety "
        "rules: never put a pin, wire or drill bit into the can or valve, and never puncture, "
        "crush, open or heat an aerosol can — no flame, no heat gun, no hot water. If it stays "
        "clogged, has no pressure, or looks damaged or leaking, stop using it and contact us at "
        "786-383-3013 or support@tropicalglitz.net.",
    ),
    (
        "Why did my paint wrinkle or crack?",
        "Wrinkling, lifting or cracking means the layers reacted with each other, dried at "
        "different rates, or went on too heavy. Wrinkling or lifting during or right after "
        "application usually comes from recoating before the previous coat flashed, coming back "
        "after the recoat window closed, heavy wet coats trapping solvent, spraying over paint "
        "that isn't dry or cured, incompatible primers, bases, reducers, activators or clears, a "
        "wrong ratio or reducer, or a strong solvent-based product over a sensitive existing "
        "finish. Cracking or fine crazing that shows up later usually means excessive film "
        "thickness, trapped solvents, an unstable or cracked finish underneath, incompatible "
        "layers, wrong ratios, substrate movement or big temperature swings. Stop spraying as soon "
        "as you see it — don't try to bury it under more base or clear. Let it dry or cure per the "
        "instructions, then remove the affected material back to a firmly attached layer and fix "
        "the cause before refinishing. Don't sand it while it's soft, swollen or gummy. Email "
        "photos to support@tropicalglitz.net or call 786-383-3013 and we'll help you pin down the "
        "cause.",
    ),
    (
        "Why is my finish rough?",
        "It depends which kind of rough. Dry spray — a sandy, dull texture — comes from holding the "
        "gun too far away, moving too fast, too much air pressure, a reducer that flashes too "
        "quickly for the temperature, or coats that are too light. Orange peel comes from air "
        "pressure, tip size, reduction, material flow, or inconsistent distance and overlap. Dust, "
        "dirt, overspray, booth contamination or debris from the air line can also land in the "
        "finish. And metal flake feels naturally textured until it's fully buried under clear — "
        "that one isn't a defect. Work out which layer is rough before doing anything. Don't try to "
        "bury heavy texture or contamination under more heavy coats. If it's flake texture, it "
        "needs enough properly applied clear over it before any sanding or polishing, and never "
        "sand into exposed flake, candy, pearl or metallic basecoat. Tell us which layer it is, "
        "your gun setup and your shop conditions and we'll narrow it down — or call 786-383-3013 / "
        "email support@tropicalglitz.net.",
    ),
    (
        "Can Tropical Glitz paint be used on engine parts?",
        "On some, yes — it depends on how hot that specific part actually gets. Many of our "
        "basecoats list heat resistance up to 400°F, but check the rating for the exact color on "
        "its product page, and remember the primer, sealer, intercoat and clear all need to handle "
        "those temperatures too: the system is only as good as its lowest-rated product. "
        "Decorative pieces like valve covers or engine covers are usually fine, as long as their "
        "real surface temperature stays inside that limit. What it's NOT for: exhaust manifolds, "
        "headers, turbochargers, catalytic components, internal engine surfaces, or anything "
        "exposed to extreme heat or direct flame. The part has to be cool, spotless and free of "
        "oil, grease, rust and silicone, and you never paint gasket surfaces, threads, electrical "
        "connections, internal passages, moving parts or mating surfaces. Let the whole system "
        "cure fully before you install the part or put heat to it. Don't go by coolant or "
        "under-hood temperature — it's the surface temperature of that part that matters. Tell us "
        "which part and which color, or call 786-383-3013, and we'll confirm it.",
    ),
    (
        "Will the sun fade the paint?",
        "Any painted finish can fade with enough sun and UV over time — how well yours holds up "
        "depends on the specific product, the clear coat over it, how much sun it sees and how "
        "it's stored. UV resistance is NOT the same across our line, so this is worth getting "
        "right. Our metal flakes are made with a UV-resistant coating. Our NEON colors are not "
        "UV-resistant and will fade with prolonged sun — neons are best for show vehicles or "
        "indoor projects. Some other specialty colors have UV limitations too, so check the "
        "product description for the exact color. A quality automotive clear with UV protection "
        "helps protect colors that are approved for outdoor use and extends the life of the "
        "finish, but clear coat cannot make a non-UV-resistant color permanently fade-proof. "
        "Good application, enough clear, a full cure, regular washing and keeping it indoors or "
        "covered when you can all help. We won't promise a number of years — climate, exposure "
        "and care vary too much. If long-term outdoor durability is critical, tell us the exact "
        "color and we'll check it, or call 786-383-3013.",
    ),
    (
        "Can I apply ceramic coating over your paint?",
        "Yes, over the CLEAR COAT once it's fully cured. Ceramic coating shouldn't go directly on "
        "basecoat, candy, pearl, metal flake or intercoat clear — those need a compatible "
        "automotive clear over them first. If the paint is fresh, follow the clear coat "
        "manufacturer's curing requirements and the ceramic coating maker's instructions; there's "
        "no universal waiting period, because cure time depends on the clear, the activator, "
        "temperature, humidity, airflow and film thickness. If the two manufacturers give "
        "different waits, use the longer one. Don't go by feel — clear that's dry to the touch is "
        "not the same as cured. Before you coat, the surface should be fully cured, clean, dry and "
        "free of wax, grease and polishing oils, and any sanding, polishing or paint correction "
        "should already be finished. Ceramic coating protects the clear and makes the surface "
        "easier to maintain, but it doesn't replace clear coat, won't fix scratches, orange peel "
        "or other defects, and won't make a neon or other non-UV-resistant color fade-proof.",
    ),
    (
        "Is Tropical Glitz paint suitable for boats?",
        "Yes, for boat parts that stay ABOVE the waterline, with proper prep and a compatible "
        "clear coat suited to the environment. Fiberglass, gelcoat, aluminum and previously "
        "painted surfaces each need different prep and primers, and the surface has to be clean, "
        "dry, firmly attached and free of wax, grease, salt, oxidation and silicone. Your primer, "
        "sealer and clear all need to be compatible with the substrate AND with the water, weather "
        "and sun the boat will see. What it's not for: anything that stays below the waterline or "
        "sits continuously submerged, unless the complete system is specifically approved for "
        "that — and it's never a substitute for antifouling bottom paint. Don't paint propellers, "
        "anodes, electrical grounding points, intakes, drains or moving parts. Let the whole "
        "system cure fully before the boat goes in the water. Tell us which part, whether it's "
        "fresh or salt water, whether the boat is trailered or kept in the water, and what the "
        "surface is, and we'll help you pick the system — or call 786-383-3013.",
    ),
    (
        "What is the Orbit Shift series?",
        "Orbit Shift is our chameleon basecoat, but with a settled tone that sets it apart from "
        "traditional chameleons. At first glance it reads like a deep, rich candy; as the viewing "
        "angle changes it shifts hard the way a chameleon does, while keeping that candy depth. "
        "Like other chameleons it has to go over a BLACK ground coat to shift properly and show "
        "the full effect — over anything else you won't get the color travel it's built for.",
    ),
    (
        "What is the Lollipop series?",
        "Lollipop is our candy basecoat line built for bright, vibrant color. It uses very fine "
        "metallic particles, which give it a smooth, silky appearance rather than a coarse "
        "sparkle. The result is a rich, bright color with a soft metallic effect — that silk-like "
        "finish is the series' signature.",
    ),
    (
        "What is the Seductive series?",
        "Seductive is a true candy basecoat: you get the rich look of a candy color with the ease "
        "of spraying a regular basecoat. It uses medium-coarse metallics for a vibrant, deep "
        "finish with plenty of dimension — the depth and intensity of a traditional candy without "
        "the complexity of a traditional candy application.",
    ),
    # --- Correcciones de entradas heredadas del sistema REP ---
    # Decían que TODAS las pinturas aguantan 400°F. La información aprobada dice
    # que el dato se confirma por color y que el sistema completo lo limita el
    # producto con la calificación más baja.
    (
        "High-temp / engine paint?",
        "Many of our colors list heat resistance up to 400°F, but it's not a blanket rating — "
        "confirm it on the product page for the exact color you're using, and remember the primer, "
        "sealer, intercoat and clear have to handle those temperatures too. The complete system is "
        "limited by whichever product has the LOWEST heat rating. That covers decorative pieces "
        "like valve covers and engine covers when their real surface temperature stays inside the "
        "limit. It does not cover exhaust manifolds, headers, turbochargers, catalytic components, "
        "internal engine surfaces or anything near direct flame — those need a dedicated "
        "high-temperature coating. If the rating for your color and clear isn't published, don't "
        "guess: call 786-383-3013 or email support@tropicalglitz.net.",
    ),
    (
        "Holds up on an engine block?",
        "It depends on how hot that surface actually gets and on your complete coating system. "
        "Many of our colors list up to 400°F, but confirm that for the exact color, and the "
        "primer, intercoat and clear have to be rated for it as well — the system is limited by "
        "its lowest-rated product. Decorative engine pieces that stay within the limit are "
        "generally fine; exhaust manifolds, headers, turbos and anything with extreme heat are "
        "not. Go by the surface temperature of the part, not the coolant or under-hood "
        "temperature, and let everything cure fully before the part goes back on and sees heat.",
    ),
    (
        "Mega Magenta Neon on brake calipers?",
        "Two things to weigh here. First, heat: calipers work if their maximum surface temperature "
        "stays inside the rating of your complete system, so confirm the rating for that color and "
        "your clear — and for racing, track use, repeated hard braking or heavy towing, a "
        "dedicated high-temperature caliper coating is the safer call. Second, and important for "
        "this one specifically: our NEON colors are not UV-resistant and will fade with prolonged "
        "sun exposure. On a daily driver that sees sun, a neon on the calipers will lose its punch "
        "over time. If it's a show car or a garage-kept build, that's less of an issue. Either "
        "way, calipers must be cool and properly prepped, never paint the pads, rotors, piston "
        "surfaces, seals, boots, bleeder screws, hose connections, slide pins or mounting "
        "surfaces, and brakes should come off and go back on by someone qualified to work on them.",
    ),
    (
        "How do I prep and paint an RC Lexan body using Tropical Glitz products?",
        "Paint it from the INSIDE — that way the Lexan stays glossy on the outside and shields the "
        "paint. Step by step: 1) Wash the inside with warm water and mild dish soap to remove "
        "mold-release agents and oils, rinse and let it dry completely. Skip aggressive solvents; "
        "they can haze or crack polycarbonate. 2) Do NOT sand or scuff the clear Lexan — scratches "
        "on the inside are visible from the outside and you lose that glossy RC finish. 3) Mask "
        "your windows and any design areas. 4) Spray Tropical Glitz The Hornet Adhesion Promoter "
        "over the clean interior — 1 to 2 light coats per the product instructions; the current "
        "specs list about 12-15 minutes before recoating under the stated conditions. It gives you "
        "a strong, flexible bond to the Lexan. 5) Apply your color or effect in multiple LIGHT "
        "coats. Heavy wet coats trap solvent and make the finish more likely to crack when the body "
        "flexes. 6) Work in reverse: the first thing you spray is what shows from outside, so with "
        "a pearl, candy, chameleon or flake the effect goes down FIRST and the ground coat goes "
        "behind it. 7) Then the ground coat — white, black or silver behind the same effect gives "
        "completely different results, so make sure it's compatible with the layers under it. "
        "8) A compatible clear as the last inside layer is optional; it seals and protects the "
        "colors. Let everything flash first and follow that clear's instructions. 9) Let the whole "
        "system dry and cure before you unmask, trim, install or flex the body. Do a test piece "
        "first to confirm your color order and ground coat — and no coating tolerates unlimited "
        "flexing, so treat a hard crash as a hard crash.",
    ),
    (
        "Does The Hornet Adhesion Promoter work on Lexan?",
        "Yes — The Hornet works on Lexan/polycarbonate RC bodies and is what we'd use as the first "
        "coating over the clean interior surface before any color. It creates a strong, flexible "
        "bond between the Lexan and the paint, which is what keeps the finish from cracking when "
        "the body flexes. Apply 1 to 2 light coats following the product instructions; the current "
        "specs list roughly 12-15 minutes before recoating under the stated conditions. Make sure "
        "the surface is clean and completely dry first — warm water and mild dish soap, no "
        "aggressive solvents — and don't sand clear Lexan, because the scratches show from the "
        "outside.",
    ),
    # Corrección de una entrada heredada del sistema REP: mandaba a usar primer
    # epóxico primero en Lexan, que es justo lo contrario de cómo se pinta por
    # dentro un cuerpo de RC transparente.
    (
        "Works with polycarbonate / Lexan bodies?",
        "Yes. On a clear Lexan RC body you paint from the INSIDE, and the first coating over the "
        "clean interior is Tropical Glitz The Hornet Adhesion Promoter — not an epoxy primer. The "
        "Hornet bonds to the polycarbonate and stays flexible so the paint moves with the body. "
        "Clean with warm water and mild dish soap, let it dry, don't sand the clear Lexan (the "
        "scratches show from outside), then 1-2 light coats of The Hornet, then your color in light "
        "coats, with the ground coat going on LAST because you're painting in reverse. A compatible "
        "clear over the inside is optional. For rigid plastic parts that aren't clear — bumpers, "
        "wings, chassis — normal plastic prep with an adhesion promoter applies instead.",
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
