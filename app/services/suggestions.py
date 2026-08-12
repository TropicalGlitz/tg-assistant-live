"""Sugerencias según la página que el cliente está viendo.

Objetivo (pedido del dueño): que el asistente se comporte como un vendedor de
piso. Si el cliente está en la ficha de "Candy Apple Red", el chat debe abrir
ofreciendo justo lo que ese cliente suele preguntar de un candy — qué base va
debajo, cómo se rocía, si necesita clear — en vez de chips genéricos.

Decisión de diseño: son REGLAS, no una llamada al modelo. Se ejecutan en
milisegundos, no cuestan tokens y son predecibles — importante porque esto se
dispara en CADA página que abre un visitante. El texto vive aquí (servidor), así
que se ajusta sin tocar el tema de Shopify.
"""
from __future__ import annotations

import re
from typing import Any

# Familias de producto -> preguntas frecuentes reales de ese tipo de producto.
# El orden importa: gana la primera familia que haga match, de lo más específico
# a lo más general (un "Candy Basecoat" debe caer en candy, no en basecoat).
FAMILIES: list[tuple[str, tuple[str, ...], list[str]]] = [
    (
        "paint_by_code",
        ("paint by code", "paint-by-code", "oem"),
        [
            "What info do you need for my code?",
            "Can you match my factory color?",
            "What sizes does it come in?",
        ],
    ),
    (
        "kit",
        ("kit",),
        [
            "What's included in this kit?",
            "What else do I need to spray it?",
            "Is this enough for my project?",
        ],
    ),
    (
        "flake",
        ("flake", "glitter"),
        [
            "How much flake do I need?",
            "What tip size and PSI for this flake?",
            "How do I spray metal flake?",
            "Do I need intercoat clear?",
        ],
    ),
    (
        "candy",
        ("candy", "kandy", "seductive", "luscious", "eclipse", "cosmic"),
        [
            "What base coat goes under this?",
            "How do I spray candy?",
            "How many coats for the color I want?",
            "Do I need clear over it?",
        ],
    ),
    (
        "pearl",
        ("pearl",),
        [
            "What base goes under this pearl?",
            "How do I spray pearl evenly?",
            "Do I need clear over it?",
        ],
    ),
    (
        "clear",
        ("clear coat", "clear", "2k high gloss", "intercoat"),
        [
            "What's the mixing ratio?",
            "How long before I can clear my base?",
            "How long before I can buff it?",
            "What tip size and PSI?",
        ],
    ),
    (
        "primer",
        ("primer", "sealer", "epoxy"),
        [
            "What's the mixing ratio?",
            "How long before I can sand it?",
            "What goes over this?",
        ],
    ),
    (
        "reducer",
        ("reducer", "activator", "retarder", "accelerator", "hardener"),
        [
            "Which one do I need for my temperature?",
            "What's the mixing ratio?",
            "How much do I need?",
        ],
    ),
    (
        "prep",
        ("adhesion promoter", "wax and grease", "degreaser", "scuff"),
        [
            "How do I prep before painting?",
            "How long before I can paint over it?",
            "Do I need this for my project?",
        ],
    ),
    (
        "basecoat",
        ("basecoat", "base coat", "single stage"),
        [
            "Do I need to reduce this?",
            "How many coats do I need?",
            "What clear should I use over it?",
        ],
    ),
    (
        "spray_can",
        ("spray can", "aerosol", "drip"),
        [
            "How many cans do I need?",
            "How do I spray it without runs?",
            "Do I need clear over it?",
        ],
    ),
]

# Chips por tipo de página cuando no hay un producto concreto.
BY_PAGE: dict[str, list[str]] = {
    "product": ["How much do I need for my project?", "How do I spray it?", "Talk to a human"],
    "collection": [
        "Help me choose a color",
        "What's the difference between these?",
        "How much paint do I need?",
    ],
    "cart": [
        "Do I qualify for free shipping?",
        "What else do I need for my project?",
        "How long is shipping?",
    ],
    "search": ["Help me find a color", "How much paint do I need?", "Talk to a human"],
    "home": ["Any promotions?", "How much paint do I need?", "Talk to a human"],
    "other": ["Any promotions?", "How much paint do I need?", "Talk to a human"],
}

_MAX_CHIPS = 4
# Palabras de envase/tamaño que no ayudan a identificar la familia del producto.
_NOISE = re.compile(r"\b(12\s*oz|ready to spray|rts|quart|gallon|pint|\d+\s*oz)\b", re.I)


def family_for(title: str) -> str | None:
    t = _NOISE.sub(" ", (title or "").lower())
    for name, keys, _chips in FAMILIES:
        if any(k in t for k in keys):
            return name
    return None


def _chips_for_family(fam: str | None) -> list[str]:
    for name, _keys, chips in FAMILIES:
        if name == fam:
            return list(chips)
    return []


# El envase es un MODIFICADOR, no una familia: un "Candy ... Spray Can" sigue
# siendo candy (la base que va debajo importa igual), pero jamás debe preguntar
# por reducción — los spray cans vienen listos para rociar.
_IS_SPRAY_CAN = re.compile(r"\bspray\s*cans?\b|\baerosol\b", re.I)
_REDUCTION_CHIP = re.compile(r"\breduce|reducer|mixing ratio\b", re.I)


def _apply_spray_can(chips: list[str]) -> list[str]:
    out = [c for c in chips if not _REDUCTION_CHIP.search(c)]
    q = "How many cans do I need?"
    if q not in out:
        out.insert(0, q)
    return out


def _short_title(title: str) -> str:
    """Nombre corto para el saludo: sin sufijos de tamaño ni separadores largos."""
    t = (title or "").strip()
    t = re.split(r"\s+[-–|]\s+", t)[0]
    t = _NOISE.sub("", t).strip(" -–|,")
    return " ".join(t.split())[:60]


def suggest(
    page_type: str = "other", product_title: str = "", collection: str = ""
) -> dict[str, Any]:
    """Devuelve {greeting, chips} para la página que el cliente está viendo.

    Nunca falla: si no reconocemos el producto, caen los chips por tipo de página.
    """
    page_type = (page_type or "other").strip().lower()
    if page_type not in BY_PAGE:
        page_type = "other"
    title = (product_title or "").strip()

    greeting = ""
    chips: list[str] = []

    if page_type == "product" and title:
        fam = family_for(title)
        chips = _chips_for_family(fam)
        if _IS_SPRAY_CAN.search(title):
            chips = _apply_spray_can(chips)
        name = _short_title(title)
        if name:
            greeting = (
                f"I see you're checking out {name} — happy to help you get it right. "
                "What can I answer?"
            )
    elif page_type == "collection" and collection.strip():
        fam = family_for(collection)
        chips = _chips_for_family(fam)
        greeting = (
            f"Browsing our {_short_title(collection)}? I can help you narrow it down."
        )

    # Relleno con los chips del tipo de página, sin repetir.
    for c in BY_PAGE[page_type]:
        if len(chips) >= _MAX_CHIPS:
            break
        if c not in chips:
            chips.append(c)

    # Siempre debe existir una salida a humano.
    if len(chips) >= _MAX_CHIPS:
        chips = chips[: _MAX_CHIPS - 1]
    if "Talk to a human" not in chips:
        chips.append("Talk to a human")

    return {"greeting": greeting, "chips": chips[:_MAX_CHIPS]}
