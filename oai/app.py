"""HTTP layer. GET and POST /oai both reach the same handler, as the spec
requires - the two must behave identically.

Thin on purpose: everything decision-shaped lives in oai/protocol.py.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request as HTTPRequest
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .basex import BaseXClient
from .config import Config
from .protocol import Provider, serialise


def create_app(config: Config, client: BaseXClient | None = None) -> Starlette:
    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app):
        own = client is None
        bx = client or BaseXClient(
            config.basex.url,
            config.basex.user,
            config.basex.password,
            timeout=config.basex.timeout,
        )
        await bx.__aenter__()
        state["client"] = bx
        try:
            yield
        finally:
            if own:
                await bx.__aexit__(None, None, None)

    async def oai(request: HTTPRequest) -> Response:
        pairs: list[tuple[str, str]] = list(request.query_params.multi_items())
        if request.method == "POST":
            form = await request.form()
            pairs += [(k, str(v)) for k, v in form.multi_items()]

        provider = Provider(config, state["client"])  # type: ignore[arg-type]
        root = await provider.handle(pairs)
        return Response(
            serialise(root),
            media_type="text/xml; charset=utf-8",
            status_code=200,
        )

    async def healthz(request: HTTPRequest) -> Response:
        bx = state["client"]  # type: ignore[assignment]
        ok = await bx.ping()  # type: ignore[union-attr]
        return PlainTextResponse("ok\n" if ok else "basex unavailable\n",
                                 status_code=200 if ok else 503)

    return Starlette(
        routes=[
            Route("/oai", oai, methods=["GET", "POST"]),
            Route("/oai/", oai, methods=["GET", "POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


def app_from_config(path: str | Path):
    return create_app(Config.load(path))


def app() -> Starlette:
    """uvicorn factory: serve the config named by OAI_CONFIG, or ./oai.toml.

        .venv/bin/python -m uvicorn oai.app:app --factory --port 8000
    """
    return create_app(Config.load(os.environ.get("OAI_CONFIG", "oai.toml")))
