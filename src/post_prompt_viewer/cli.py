"""Command-line entry point: run the service with uvicorn."""

from __future__ import annotations

import argparse

from .config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="post-prompt-viewer",
        description="Inspect SignalWire AI Agent post_prompt conversations.",
    )
    parser.add_argument("--host", default=settings.host, help="bind host")
    parser.add_argument("--port", type=int, default=settings.port, help="bind port")
    parser.add_argument("--reload", action="store_true", help="auto-reload (development)")
    parser.add_argument("--workers", type=int, default=1, help="number of worker processes")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "post_prompt_viewer.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
