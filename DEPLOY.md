# Despliegue — Tropical Glitz AI Assistant (reemplazo de REP)

Orden de operaciones para ir en vivo. Marca lo que ya tengas.

## 0. Lo que necesito de ti (secretos)
Ninguno se sube al repo; van en variables de entorno del host.

- [ ] **Supabase**: crear proyecto → copiar `DATABASE_URL` (connection string, modo `asyncpg`).
- [ ] **Anthropic**: `ANTHROPIC_API_KEY` (para Claude). *Única IA externa.*
      Los embeddings corren LOCALES en el backend (fastembed/bge-small) — sin API key.
- [ ] **Shopify Custom App** (panel → Settings → Apps → Develop apps → Create):
      scopes `read_products`, `write_products`, `read_orders`, `read_inventory`
      (inventario en variantes). Los **metafields** de producto se leen con `read_products`;
      si algún metafield está en un namespace protegido, habilita también su acceso en
      Settings → Custom data. Instalar y copiar:
      `SHOPIFY_ADMIN_TOKEN` (shpat_…), `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET` (shpss_…).
      En Custom Apps, `SHOPIFY_WEBHOOK_SECRET == SHOPIFY_API_SECRET`.

## 1. Base de datos (una vez)
```bash
psql "$DATABASE_URL" -f sql/001_init_pgvector.sql
psql "$DATABASE_URL" -f sql/002_kb_faqs_analytics.sql
psql "$DATABASE_URL" -f sql/003_promotions.sql
```

## 2. Desplegar el backend
Cualquier host de contenedores (Render / Railway / Fly.io / VPS). Con el Dockerfile incluido:
```bash
docker build -t tg-assistant .
docker run -p 8000:8000 --env-file .env tg-assistant
# healthcheck:
curl https://tu-backend.com/health   # -> {"status":"ok"}
```
Copia `.env.example` → `.env` y complétalo (§0).

## 3. Cargar el conocimiento (una vez)
```bash
python -m scripts.backfill_catalog          # catálogo Shopify -> product_vectors
python -m scripts.import_rep_faqs data/rep_faqs_full.md   # 178 FAQs -> faqs
python -m scripts.import_pdfs data/pdfs                    # PDFs -> kb_chunks (7/29 incluidos)
```

## 4. Webhooks en tiempo real
```bash
python -m scripts.register_webhooks https://tu-backend.com
# registra products/create|update|delete -> /webhooks/shopify/*
```

## 5. App Proxy (para el widget)
Panel Shopify → tu app → **App proxy**:
- Subpath prefix: `apps`  ·  Subpath: `assistant`
- Proxy URL: `https://tu-backend.com`
Así el widget llama `https://tropicalglitz.net/apps/assistant?...` y Shopify lo reenvía
firmado a `GET /apps/assistant` (verificado por HMAC en `security.verify_app_proxy_signature`).

## 6. Widget en la tienda (Theme App Extension)
```bash
npm i -g @shopify/cli @shopify/theme
cd extensions/tg-assistant && shopify app deploy
```
Luego: Online Store → Themes → Customize → **App embeds** → activar
"Tropical Glitz AI Assistant" (ajusta color/título/greeting ahí).
> Prueba visual sin desplegar: abre `widget/demo.html` (modo demo con FAQs locales;
> pega la URL del backend para probar el streaming real).

## 7. Verificación
- [ ] `/health` responde ok.
- [ ] Webhook de prueba valida HMAC (ver README, bloque `openssl`).
- [ ] El widget abre, saluda proactivo, responde con streaming y cita fuentes.
- [ ] Pregunta de order-status pide nº de pedido y responde con datos reales (no alucina).
- [ ] Pregunta fuera de dominio → escala a contacto (umbral 0.60).

## Estado actual del build
- ✅ Fase 1: esquema 3 colecciones + retrieval híbrido + system prompt REP + umbral/handoff.
- ✅ Fase 2: backfill catálogo + import 178 FAQs. (Pendiente: chunker de los 29 PDFs.)
- ✅ Fase 3: App Proxy firmado + webhooks + streaming SSE + cart-context.
- ✅ Fase 4: ruta determinista order-status + **13 triggers proactivos** (`/apps/proactive`) + promos con fecha.
- ✅ Fase 5: Theme App Extension (App Block) + demo visual con proactividad.

### Pendiente para el 100% absoluto
- ✅ Chunker de PDFs listo. Incluidos 7/29 en `data/pdfs/`; agrega los 22 restantes ahí y re-corre `import_pdfs`.
- Configurar el App Proxy de Shopify también para `/apps/proactive` (mismo proxy URL).
