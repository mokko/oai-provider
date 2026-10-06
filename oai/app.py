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
from .protocol import Provider, serialise, xslt_problem


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
        # One Provider per app, not per request: it builds a QueryBuilder whose
        # rendered queries are cached, so rebuilding it on every request threw
        # that cache away and re-read the templates from disk each time.
        state["provider"] = Provider(config, bx)
        try:
            # Fail early, not on the first request that asks for it: a format
            # that needs Saxon must not be advertised by a server that cannot
            # produce it.
            stylesheet_formats = [f for f in config.formats if f.kind == "xslt"]
            if stylesheet_formats:
                problem = await xslt_problem(bx)
                if problem:
                    raise RuntimeError(
                        "config advertises "
                        + ", ".join(f.prefix for f in stylesheet_formats)
                        + ' (kind "xslt") but BaseX cannot run XSLT 2.0+: '
                        + problem
                    )
            yield
        finally:
            if own:
                await bx.__aexit__(None, None, None)

    async def oai(request: HTTPRequest) -> Response:
        pairs: list[tuple[str, str]] = list(request.query_params.multi_items())
        if request.method == "POST":
            form = await request.form()
            pairs += [(k, str(v)) for k, v in form.multi_items()]

        provider = state["provider"]
        root = await provider.handle(pairs)  # type: ignore[union-attr]
        return Response(
            serialise(root, config.protocol.pretty),
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
