"""Async BaseX REST client.

Starlette's event loop must never block, and BaseX's own HTTP layer is the
concurrency here: an httpx connection pool dispatches queries and Java does
the work off-loop. That is the honest reason this shape suits asyncio - it is
not that the XML work is cheap, it is that it is not ours.

Transport is BaseX's documented POST shape:

    <query xmlns="http://basex.org/rest">
      <text><![CDATA[ ...xquery... ]]></text>
      <variable name="db" value="..."/>
    </query>

Values are escaped into the XML, so a stray & or < in a file name cannot
corrupt the request.
"""

from __future__ import annotations

import httpx
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

REST_NS = "http://basex.org/rest"


class BaseXError(Exception):
    """BaseX reported a failure."""


class BaseXClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._auth = (user, password)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> BaseXClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/query+xml; charset=utf-8"},
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("client used outside its context manager")
        return self._client

    # -- transport --------------------------------------------------------

    @staticmethod
    def _body(query: str, variables: dict[str, object]) -> bytes:
        parts = [
            f'<query xmlns="{REST_NS}">',
            f"<text><![CDATA[{query}]]></text>",
        ]
        for name, value in variables.items():
            parts.append(
                f"<variable name={quoteattr(str(name))} "
                f"value={quoteattr(escape(str(value)))}/>"
            )
        parts.append("</query>")
        return "".join(parts).encode("utf-8")

    async def _post(self, query: str, variables: dict[str, object]) -> str:
        resp = await self.client.post(self.url, content=self._body(query, variables))
        if resp.status_code >= 400:
            raise BaseXError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")
        return resp.text

    # -- operations -------------------------------------------------------

    async def query(self, xquery: str, **variables: object) -> str:
        """Run a query, return raw serialised output."""
        return await self._post(xquery, variables)

    async def query_xml(self, xquery: str, **variables: object) -> ET.Element | None:
        """Run a query whose result is a single well-formed document."""
        text = await self.query(xquery, **variables)
        if not text.strip():
            return None
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise BaseXError(f"response was not XML: {exc}\n{text[:300]}") from exc

    async def query_nodes(
        self, xquery: str, **variables: object
    ) -> list[ET.Element]:
        """Run a query that returns a *sequence* of nodes.

        An XQuery `for ... return <x/>` returns N top-level elements, which is
        not a well-formed document and cannot be parsed as one. ListIdentifiers
        is exactly this shape, so it is worth having in the client rather than
        rediscovering it in the protocol layer.
        """
        text = await self.query(xquery, **variables)
        text = text.strip()
        if not text:
            return []
        # a stray XML declaration would be illegal inside a wrapper
        if text.startswith("<?xml"):
            text = text.split("?>", 1)[1].lstrip()
        try:
            return list(ET.fromstring(f"<wrap>{text}</wrap>"))
        except ET.ParseError as exc:
            raise BaseXError(f"response was not XML: {exc}\n{text[:300]}") from exc

    async def command(self, cmd: str) -> str:
        resp = await self.client.get(self.url, params={"command": cmd})
        if resp.status_code >= 400:
            raise BaseXError(f"HTTP {resp.status_code}: {resp.text.strip()[:500]}")
        return resp.text

    async def ping(self) -> bool:
        """True if the server answers a trivial expression.

        Note the port: BaseX 12 uses 8080 for REST. Older docs say 8984 and
        are wrong for this build.
        """
        try:
            return (await self.query("1 to 3")).strip() == "1\n2\n3"
        except Exception:
            return False

    async def create_database(self, name: str) -> None:
        await self.command(f"CREATE DB {name}")

    async def database_exists(self, name: str) -> bool:
        text = await self.query(f"db:exists({name!r})")
        return text.strip() == "true"

    async def drop_database(self, name: str) -> None:
        await self.command(f"DROP DB {name}")

    async def count_documents(self, database: str) -> int:
        """How many documents a database holds."""
        text = await self.query(f"count(collection({database!r}))")
        return int(text.strip() or 0)
