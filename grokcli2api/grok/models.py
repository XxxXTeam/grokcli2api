"""Static catalog of Grok model identifiers exposed via ``/v1/models``.

These model strings follow the same conventions xAI uses on grok.com: short
slugs with ``-`` separators (``grok-4``, ``grok-3``, ``grok-code-fast-1``).
Treat the list as a discovery surface -- the official CLI lets the user pick
from the same identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GrokModel:
    id: str
    owned_by: str = "xai"
    family: str = "grok"
    description: str = ""


# Curated set. Add new entries here as xAI ships new variants. Names match
# what the official Grok CLI exposes; the underlying upstream id is often
# ``grok-build`` for the strongest tier per the HAR capture.
MODEL_CATALOG: list[GrokModel] = [
    GrokModel("grok-build", description="Grok 4.5 / build tier (official default)"),
    GrokModel("grok-4", description="Alias of grok-build (OpenAI-compat)"),
    GrokModel("grok-4.5", description="Alias of grok-build (OpenAI-compat)"),
    GrokModel("grok-auto", description="Server-side picker (uses link-query heuristics)"),
    GrokModel("grok-4-fast-reasoning", description="Grok 4 fast reasoning tier"),
    GrokModel("grok-4-fast-non-reasoning", description="Grok 4 fast non-reasoning"),
    GrokModel("grok-3", description="Grok 3 baseline"),
    GrokModel("grok-3-mini", description="Grok 3 mini (smaller / cheaper)"),
    GrokModel("grok-code-fast-1", description="Code-tuned variant"),
    GrokModel("grok-2-vision", description="Legacy multimodal"),
]


def list_model_ids() -> list[str]:
    return [m.id for m in MODEL_CATALOG]


def find_model(model_id: str) -> GrokModel:
    """Return a :class:`GrokModel` for a given id, falling back to a generic entry."""

    for m in MODEL_CATALOG:
        if m.id == model_id:
            return m
    return GrokModel(model_id, description="unknown / custom")


def upstream_model_id(friendly: str) -> str:
    """Translate an OpenAI-compat model id to the per-session upstream id.

    See :data:`grokcli2api.openai.converter._MODEL_ALIASES`. Exposed
    here as a public helper so server-side routing and telemetry don't
    need to import the converter.
    """

    from grokcli2api.openai.converter import _upstream_model
    return _upstream_model(friendly)
