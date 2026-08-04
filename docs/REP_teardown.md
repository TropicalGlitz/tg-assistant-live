# REP.ai — Teardown técnico y de producto (referencia para el reemplazo)

> Tienda: **Tropical Glitz** (`tropicalglitz.net`, `7b297d-7a.myshopify.com`)
> Proveedor analizado: **Rep AI** (`app.hellorep.ai` / backend `server.myrepai.com`)
> Fecha del análisis: 2026-08-03. Fuentes: widget en producción (runtime + red) + captura del dashboard Home.

---

## 1. Arquitectura real de REP (ingeniería inversa del widget)

Hallazgo central: **REP no corre un RAG propio; orquesta la conversación con Voiceflow.**

| Capa | Qué usa REP | Evidencia |
|------|-------------|-----------|
| Inyección en la tienda | `<script id="repLoader">` → globals `initRep`, `repSettings`, `rep`, `repAppV2`, `repCT`. Patrón Theme App Extension / App Block. | Runtime del DOM |
| Widget (frontend) | SPA con code-splitting servida desde CloudFront `d1o5e9vlirdalo.cloudfront.net/client/prod/2.0.227/`. Chunks: `Messages`, `WatchCartChanges`, `CartDrawerWatch`, `cartState`, `PollingIsUserLoggedIn`, `autoTracking`, `PageViewEvent`, `tracking-core`, `purify` (sanitiza HTML del LLM), `linkify-html`, `draggable`. | performance.getEntriesByType |
| Sesiones / historial | Backend propio `server.myrepai.com` (`GET /web/conversations/recent`). | Interceptor de red |
| Motor conversacional (IA) | **Voiceflow**, vía App Proxy de Shopify: `tropicalglitz.net/apps/vf-proxy` (`vf` = Voiceflow). | Interceptor de red |

### API JS pública (`window.rep`)
`sendUserMessage`, `triggerFlow`, `triggerEvent`, `showProactive`, `switchPartner`,
`resetConversation`, `search`, `setStyle`, `stopAllPolling`, `resumeAllPolling`,
`open/close`, `hide/show`, `enable/disable`, `config`, `fullScreenToggle`,
`theaterModeToggle`, `setAICorrect`, `changeTarget`, `refreshSession`, `freshStart`.

### Config del widget (`repSettings`)
- Color marca `#ef2c8f` (rosa), fondo `#FFFFFF`, posición `BOTTOM_RIGHT` (20px/20px), botón `CHAT_ICON` tamaño `SMALL`.
- `cid` (cliente), `sid` (sesión), shop `7b297d-7a.myshopify.com`.
- Integración declarada: `ECOMMERCE → SHOPIFY`.
- Título "Tropical Glitz AI Support" / "Here to help you shop".
- Diccionario de strings UI: "Out of stock", "Selling fast!", etc.
- Transcript renderizado **aislado** (shadow/iframe) — no legible desde el top document (contenido capturado por screenshot).

---

## 2. Comportamiento observado (prueba en vivo como cliente)

Consulta de prueba: *"candy red profundo sobre base plata para un tanque de moto — qué productos y cuánto necesito"*.

1. **Prioriza upsell antes de responder.** Abre con "Welcome back!" (repetido) y empuja "Candy & Candy Concentrates" de forma genérica; disparó una promo de *"Independence Day look tonight"* **fuera de temporada (3 de agosto)** → lógica proactiva/estacional mal calibrada.
2. **No aterriza la respuesta técnica.** A la pregunta de mayor intención de compra respondió con preguntas de calificación genéricas y, en sesión limpia, **se quedó >2 minutos en "escribiendo" sin devolver recomendación concreta** (SKU/cantidad). Latencia alta / posible timeout del proxy a Voiceflow.

**Conclusión:** su punto fuerte es la capa de proactividad/analytics; su punto débil es el grounding real en catálogo y la latencia. Ahí gana tu build.

---

## 3. Dashboard REP (Home) — lectura de la captura

**KPIs (periodo vs anterior):**
- AI-generated sales: **$24,452** (+15.9% vs $21,096)
- Deflected Support Tickets: **361** (+22.4% vs 295)
- Conversations Resolved by AI: **97.9%** (+2.5% vs 95.48%)

**AI Maturity Score: 67/112 (ADVANCED, 60%).** "Ahead of 94% of similar stores; top performers 85%+".
- Foundation **30/35**: Tone of Voice ✓(+10), Logo ✓(+5), Connect First Integration ✓(+5), Immersive Default View ⧗(+5, pendiente).
- Sales **29/37**.
- Support **8/40** ← muy bajo, es donde REP dice que hay más upside.

**Conversation Overview:** 0 en vivo · **1,190 conversaciones totales** · 2 reportadas como no útiles.
**Conversation Initiator:** Proactive AI **60%** / Shopper **40%** (confirma el sesgo proactivo del bot).
**Canales:** Website (Active), Facebook (Active), Instagram (Active), WhatsApp (inactivo/Activate), Email.
**Otros módulos:** Shopper Recovery, AI Recommendations (18 nuevas).

**Navegación del panel:** Home · Conversations · Inbox (New) · AI Sales · AI Support · AI Insights · AI Training · Settings · Expert Zone.

Posicionamiento de producto (mockup del login): *"I'm your personal shopping concierge… See top sellers in athleisure… Btw, what's your size?"* → REP se vende como **concierge proactivo de compra**, no como soporte reactivo.

---

## 4. Mapeo REP → tu arquitectura (qué replicar y qué mejorar)

| Capacidad REP | Tu equivalente (FastAPI + pgvector + Claude) | Nota |
|---|---|---|
| Widget App Block | Theme App Extension propia | Mismo patrón, ya planificado |
| App Proxy `/apps/vf-proxy` | **App Proxy `/apps/tg-assistant`** → tu backend | Evita CORS abierto, hereda sesión Shopify. **A añadir al scaffold.** |
| Motor Voiceflow | RAG pgvector + Claude con streaming SSE | Tu ventaja: grounding real + baja latencia |
| Cart watch (`WatchCartChanges`) | Listener de `/cart.js` + contexto en el prompt | **A añadir**: "veo X en tu carrito, para candy necesitas clear" |
| Proactividad (60% AI) | Reglas de trigger server-side (no en flujo externo) | Control de estacionalidad correcto |
| Deflection de soporte | RAG sobre FAQ/policies + escalado a humano | Support 8/40 = mayor oportunidad |
| Analytics / Maturity | Tabla de eventos + métricas (sales asistidas, deflection) | Fase 2 |

**Decisiones que copiar de REP:** App Proxy, cart-context, sanitización del HTML del LLM (`purify`), y proactividad — pero disparada por tus reglas.

---

## 5. Panel interno de REP (análisis en vivo, sesión autenticada)

Acceso directo al dashboard `app.hellorep.ai` (cuenta Tropical Glitz, usuario "Manny").
Rendimiento all-time desde Abr 2024: **$540,591 en ventas asistidas**, **5,754 tickets deflectados**, **ROI mensual promedio 36.6×**, **1,127–1,190 conversaciones**.

### 5.1 AI Personality (= el "system prompt" de REP)
Configuración exacta que define cómo responde el bot:

- **Tono de voz:** "Friendly, down to earth, secure, confident".
- **Marca:** "Tropical Glitz is all about bringing vibrant, high-quality custom paint to life. We are known for our bright paints and candy colors as well as our vast selection of metal flakes. We value our customers and are here to help them by providing great support through tutorials and personalized customer service."
- **Clientes:** "Our customers are deeply passionate about their vehicles, motorcycles, and other do it yourself projects. They take pride in their work and seek unique ways to stand out. Some are new al custom paint; muchos son pintores profesionales detail-oriented y exigentes…"
- **Nivel de detalle:** Detailed (normal).
- **Global AI instructions (guardrail + escalado):** "Handle customer inquiries in a friendly, professional, and helpful manner. **If your confidence score for the answer is below 60%, consider yourself stuck.** If you detect keywords like 'I need help,' 'I don't understand,' or 'This isn't working,' recognize the need for escalation. Let the customer know that you are sorry… please visit our FAQ section or contact support. Email: **tropicalglitz@gmail.com** / Tel: **786-383-3013**."

> Mapea 1:1 a `SYSTEM_PROMPT` en `app/services/rag.py`: persona + umbral de confianza (0.60) + reglas de escalado por keywords + datos de contacto de fallback.

### 5.2 Knowledge Base (el corpus real del RAG)
Cuatro fuentes, todas con **contador "Times used"** (= nº de recuperaciones del RAG):

- **File Sources — 29 PDFs** (guías técnicas). Top por uso: *Recommended Paint Quantity Based on Project Size* (717), *Candy Basecoats* (701), *Reducers* (376), *Metal Flake Requirements by Project Size* (347), *Recommended Tip Sizes by Flake Size* (328), *Candy and Candy Concentrates* (321), *Flake Matched Basecoat* (240), *Basecoats* (231)… Límites: solo PDF/CSV; PDF ≤800 palabras / 5-6 párrafos; CSV ≤3000 filas (1 FAQ por fila).
- **Custom FAQs — 178 Q&A curadas a mano.** Top: *How much paint do I need to paint a car?* (378), *Do I need to prime before painting?* (355), *Available flake sizes?* (326), *Is this base coat clear coat?* (318), *2oz/4oz ready-to-spray en lata o para pistola?* (317), *How many spray cans for my car?* (316), *Metal flake en clear coat?* (292), *How much clear coat?* (239).
- **Estructura de cada FAQ:** `pregunta` + `respuesta` (≤3000 chars) + `producto asociado` (opcional) + **acción posterior**: `Offer further assistance` / `Recommend a product` / `Recommend a collection` / `Continue to flow`.
  - Ej.: "How much paint do I need to paint a car?" → "Small cars ≈ 2-3 quarts, medium ≈ 1 gallon, large ≈ 1.5-2 gallons."
- **URL Sources** + **Promotions.**

> Este es el verdadero cerebro para esta tienda: **catálogo Shopify + 29 PDFs + 178 FAQs**. Para el reemplazo hay que **ingerir estas mismas fuentes en pgvector** junto a los productos (una tabla `kb_chunks` además de `product_vectors`). El contador "Times used" es un ranking de utilidad que podemos replicar como métrica.

### 5.3 Promotions
Una sola promo: **"USA20 / 4th JULY SALE" → Expired**. Confirma el fallo detectado en la prueba: el bot seguía empujando un "Independence Day look" el 3 de agosto porque **arrastra una promo vencida**. Lección: expiración de promos gestionada por fecha en tu backend.

### 5.4 AI Sales → Sales Skills (motor de proactividad, el 60%)
13 "skills" conductuales, cada uno toggle + Settings, disparados por **tipo de página + estado de engagement**:
Virtual Try-On (beta, off) · Subscribe & Discount (on) · Product Finder (busca catálogo + preguntas de seguimiento) · Disengaged en homepage (visitante nuevo / recurrente / cliente que vuelve) · Disengaged tras ver carrito · Disengaged en página de producto · Disengaged en producto agotado · Disengaged en colección · Convert desde cualquier otra página · **Upsell tras add-to-cart** · **Convert abandoned carts**.

> Replicable como **reglas de trigger server-side** sobre eventos de página/carrito (page_type, is_returning, cart_state, stock). Aquí es donde tu control de estacionalidad evita el fallo de la promo vencida.

### 5.5 Integrations (activas vs disponibles)
**Activas** (marcadas "See details"): **Shopify, Instagram, Facebook, Klaviyo**. WhatsApp = inactivo ("Connect").
Catálogo disponible por categoría: Reviews (Yotpo/Judge.me/Okendo), **Handoff a humano** (Gorgias/Zendesk/Help Scout/Richpanel/Freshdesk/Kustomer/Salesforce Service Cloud), Loyalty (Smile.io/LoyaltyLion/Yotpo Loyalty), Email (Klaviyo/Attentive/Postscript/Mailchimp/Omnisend/Listrak), Returns (Loop), Order status (ShipAny/PDQ), Mobile (Tapcart), Rep Inbox (bandeja compartida email+chat).

### 5.6 Conversations (calidad real)
Transcripción real (Oklahoma City, 2 Ago): apertura **proactiva** en homepage → *"Evening from Tropical Glitz: we've got bold candy paints and metal flakes… I can help highlight top picks that fit your style and budget."* Incluye **chips de respuesta sugerida** (What's recommended? / free shipping threshold / add to cart / application step…), **tracking de páginas visitadas** (Homepage, Spray Guns & Airbrushes, TG Flake Gun), y por cada mensaje del AI: botones **"Sources"** (citas del RAG) y **"Show reasoning"**. Herramienta "Correct the AI" para feedback → reentrena FAQs.

---

## 6. Plan de reemplazo — implicaciones concretas

1. **Corpus RAG = 3 fuentes, no solo productos.** Tablas: `product_vectors` (catálogo) + `kb_chunks` (los 29 PDFs troceados) + `faqs` (las 178 Q&A con producto asociado y acción posterior). Todo a pgvector.
2. **System prompt** portado de 5.1: persona + umbral 0.60 + escalado por keywords + contacto de fallback (email/tel).
3. **Proactividad server-side** replicando los 13 triggers de 5.4, con expiración de promos por fecha (arregla el bug de 5.3).
4. **Streaming SSE** para matar la latencia que observé en el widget.
5. **Handoff a humano** cuando confianza <0.60 o keywords de escalado (como REP), vía email/inbox.
6. **Citas + "Show reasoning"** guardando qué chunks se recuperaron por respuesta (para depurar y para el contador estilo "Times used").

Fuentes internas verificadas en vivo el 2026-08-03 (dashboard `app.hellorep.ai`, cuenta Tropical Glitz).

---

## 7. Datos profundos (extracción vía API/estado autenticado)

### 7.1 Corpus real extraído
- **178 FAQs completas** (pregunta + respuesta + `timeUsed` + productos recomendados) → guardadas en `data/rep_faqs_full.md`. Endpoint: `GET server.hellorep.ai/partner/tropicalglitz/faqs` (devuelve las 178 en una respuesta). Cada registro incluye `question`, `answer` (HTML), `timeUsed`, `relatedProduct`, `recommendedProducts`, `continueToFlow`, `fileUrl` (s3://rep-personalize/...), y campos de embedding `vector` + **`cohereVectorV4`** → REP vectoriza con **Cohere embeddings v4**.
- **29 PDFs** en File Sources (guías técnicas), top-8 documentados en §5.2.

### 7.2 Distribución real de conversaciones (Insights → Topics)
Sesiones distintas por tema y **handoffs a humano** (dónde falla el bot). Total ~1,190 conversaciones:

| Tema | Sesiones | Handoffs a humano |
|------|---------:|------------------:|
| PRODUCT_RELATED_QUESTIONS | 525 | 9 |
| NO_TOPIC | 193 | 0 |
| PRODUCT_AVAILABILITY | 131 | 6 |
| ADD_TO_CART | 94 | 1 |
| PRICE_QUESTION | 70 | 1 |
| **CONTACT_DETAILS** | 41 | **28** |
| ITEM_RECOMMENDATION | 38 | 0 |
| DISCOUNT_SALES_QUESTION | 36 | 11 |
| SHIPPING_QUESTIONS | 34 | 3 |
| REDIRECT_TO_CHECKOUT | 28 | 0 |
| **CUSTOMER_SUPPORT** | 27 | **19** |
| **ORDER_STATUS** | 20 | **11** |
| PRODUCT_MATERIALS | 18 | 0 |
| TRACKING | 11 | 4 |
| (otros: international shipping, damaged order, cancellation, order delays, refunds…) | — | — |

> **Insight de producto clave:** el 44% del volumen es PRODUCT_RELATED (525) — dominio del RAG de catálogo/FAQs. Pero los **handoffs se concentran en CONTACT_DETAILS (28), CUSTOMER_SUPPORT (19), ORDER_STATUS (11), DISCOUNT (11)** — áreas donde REP no resuelve. Tu build gana justo ahí: order status vía Admin API de Shopify, contacto/soporte determinista, y descuentos vía lógica de promos. Reducir esos handoffs es el ROI diferencial.

### 7.3 Taxonomías de REP (para replicar analytics)
- **Session tags (13):** Fallback response (DIDNT_GET_IT), Redirected to product/checkout/collection, Added to cart by AI, Reported helpful/unhelpful, Contact support, Resolved by AI, Shopper/AI initiated, Lead collected, AI-generated order, Ticket consumed.
- **Shopper emotions (10):** High Interest, Needs Assistance, Looking Forward, Seeking Clarity, Positive Experience, Unexpected Discovery, Urgency, Reassured, Brand Confidence, Expectation Mismatch.
- **Customer problems (21):** lack of trust, poor navigation, high shipping cost, forced account creation, **lack of product information** (AI no supo responder), hidden costs, etc.
- **30 tipos de topic** de clasificación de conversación (arriba).

### 7.4 Chat Widget (config exacta para replicar la UX)
- Tabs de config: Basics · Chat experience · Position · Button · Embedded widgets · Menu · Settings.
- Colores: Primary `#ef2c8f`, Secondary `#f3f3f3`, Background `#FFFFFF`, Chip text/border `#ef2c8f`.
- Título "Tropical Glitz AI Support" / subtítulo "Here to help you shop". Modo apertura: Regular chat box. Theater Mode: off.
- Mensaje proactivo + chips: "Welcome back! How may I be of service to you today?" → chips "Any promotions?" / "I'm not sure what to choose".

### 7.5 AI Support (submódulos)
Support Analytics · Support Skills · Email Answering · Social DM Answering · **Missing Information (32)** ← 32 preguntas que el bot no supo responder = backlog directo para enriquecer el KB. Rep Inbox unifica email + chat.

### 7.6 Rendimiento (Home, all-time desde Abr 2024)
Ventas asistidas **$540,591** · Tickets deflectados **5,754** · ROI mensual **36.6×** · 97.9% resueltas por IA · AI Maturity 67/112 (Support 8/40 = mayor upside).

---

## 8. Especificación de datos para el build (pgvector)

Tres colecciones vectoriales + tablas de soporte:

- **`product_vectors`** — catálogo Shopify (ya definido en §schema): `product_id` PK, `embedding vector(1536)`, `payload jsonb`, `content_hash`.
- **`faqs`** — las 178 Q&A: `id`, `question`, `answer`, `embedding vector(1536)` (sobre la pregunta+sinónimos), `recommended_skus text[]`, `related_product_id`, `post_action` (offer_assistance|recommend_product|recommend_collection|continue_flow), `time_used int` (boost), `source` (‘rep_import’), `content_hash`.
- **`kb_chunks`** — los 29 PDFs troceados (≤800 palabras): `id`, `doc_name`, `chunk_idx`, `text`, `embedding vector(1536)`, `content_hash`.
- **`conversations` / `messages` / `events`** — sesiones + tags (taxonomía §7.3) + page-visit tracking + handoff flag, para replicar analytics y "Times used".

Retrieval híbrido: la query embebe y busca en las 3 colecciones (products + faqs + kb_chunks) con pesos; las FAQs con `time_used` alto reciben boost. Si `score < 0.60` o el topic ∈ {ORDER_STATUS, CONTACT_DETAILS, CUSTOMER_SUPPORT} → ruta determinista (Admin API / contacto) o handoff, en vez de alucinar.
