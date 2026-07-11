"""Reverse-engineered Grok CLI client chain.

The Grok CLI itself speaks to ``https://cli-chat-proxy.grok.com/{v1}/...``.
That endpoint accepts OpenAI-shaped payloads; the CLI then decorates every
request with the ``x-grok-*`` custom headers described in
:doc:`ANALYZE_REPORT`. We replicate that decoration here.
"""

from grokcli2api.grok.client import GrokAPIError, GrokClient
from grokcli2api.grok.headers import build_grok_headers
from grokcli2api.grok.models import GrokModel, MODEL_CATALOG, list_model_ids

__all__ = [
    "GrokAPIError",
    "GrokClient",
    "build_grok_headers",
    "GrokModel",
    "MODEL_CATALOG",
    "list_model_ids",
]
