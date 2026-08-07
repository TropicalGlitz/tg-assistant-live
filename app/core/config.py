"""Configuración central. Todo se lee de variables de entorno (12-factor)."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "development"
    log_level: str = "INFO"

    # --- Shopify ---
    shopify_shop_domain: str = Field(..., description="mi-tienda.myshopify.com")
    shopify_api_key: str
    shopify_api_secret: str          # usado para el HMAC de OAuth/App
    shopify_admin_token: str         # Admin API access token de la Custom App
    shopify_api_version: str = "2025-07"
    # Secreto que firma los webhooks. En Custom Apps == API secret key.
    shopify_webhook_secret: str

    # --- Base de datos (Supabase / Postgres + pgvector) ---
    database_url: str  # postgresql+asyncpg://user:pass@host:5432/db

    # --- Embeddings (LOCALES, sin API key) ---
    # fastembed/ONNX en CPU dentro del backend. Única IA externa = Anthropic (Claude).
    embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384 dims
    embedding_dim: int = 384

    # --- LLM (Anthropic / Claude) ---
    anthropic_api_key: str
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = 1024

    # --- Retrieval ---
    top_k: int = 6
    min_similarity: float = 0.25  # umbral de corte del score de similitud

    # --- Admin (panel de conversaciones) ---
    # Token para abrir /admin/conversations?key=... Si queda vacío, el panel se
    # deshabilita (responde 401). Poner un valor secreto propio en Render.
    admin_token: str = ""

    # --- Contacto / handoff por email (Resend) ---
    # API key de resend.com. Si queda vacío, /contact responde 503 y el widget
    # muestra el email/teléfono como alternativa. Poner en Render como RESEND_API_KEY.
    resend_api_key: str = ""
    # Remitente. Debe ser una dirección de un dominio verificado en Resend
    # (p. ej. "Tropical Glitz <assistant@tropicalglitz.net>"). Override: CONTACT_FROM_EMAIL.
    contact_from_email: str = "Tropical Glitz Assistant <onboarding@resend.dev>"
    # Destino: bandeja del equipo de soporte. Override: CONTACT_TO_EMAIL.
    contact_to_email: str = "support@tropicalglitz.net"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
