"""`python -m grokcli2api` CLI entry point.

Bootstraps the FastAPI server with the settings from environment / .env.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from grokcli2api.config import Settings
from grokcli2api.utils.logger import configure_logging, get_logger

log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grokcli2api",
        description="Run an OpenAI-compatible HTTP server powered by the Grok CLI chain.",
    )
    parser.add_argument("--host", help="bind host (overrides GROK2API_HOST env)")
    parser.add_argument("--port", type=int, help="bind port (overrides GROK2API_PORT env)")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="enable auto-reload (development use)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="number of uvicorn workers (default: 1 -- required for streaming)",
    )
    return parser


def main() -> None:
    settings = Settings()  # reads env / dotenv
    configure_logging(settings.log_level)

    parser = _build_parser()
    args = parser.parse_args()

    host = args.host or settings.host
    port = args.port or settings.port

    log.info(
        "starting grokcli2api v%s on http://%s:%s",
        settings.app_version,
        host,
        port,
    )
    log.info("upstream chat proxy: %s", settings.chat_proxy_base_url)
    auth_source = (
        "session_token"
        if settings.session_token
        else ("auth_file=" + str(settings.auth_file) if settings.auth_file else "missing")
    )
    log.info("auth source: %s", auth_source)

    # Validate the auth_file path eagerly so a typo in .env surfaces during
    # startup, not during the first chat request.
    if settings.auth_file and not settings.session_token:
        af = settings.auth_file
        if not af.exists():
            log.warning(
                "GROK_AUTH_FILE points to a path that does not exist: %s "
                "(check .env for stray trailing characters / missing newline)",
                af,
            )
        elif af.is_dir():
            log.warning("GROK_AUTH_FILE is a directory, expected a JSON file: %s", af)
        elif af.suffix.lower() != ".json":
            log.warning(
                "GROK_AUTH_FILE does not end with .json (suffix=%s) -- "
                "verify the path in .env isn't truncated: %s",
                af.suffix,
                af,
            )
    if settings.grok_proxy_url:
        log.info(
            "outbound proxy: %s (no_proxy=%s)",
            settings.grok_proxy_url,
            settings.no_proxy_patterns or "[]",
        )
    else:
        log.info("outbound proxy: <none, honouring HTTP_PROXY/HTTPS_PROXY env>")

    uvicorn.run(
        "grokcli2api.server:app",
        host=host,
        port=port,
        reload=args.reload,
        workers=args.workers or 1,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("interrupted, shutting down")
        sys.exit(130)
