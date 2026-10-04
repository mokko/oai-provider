#!/usr/bin/env python3
"""oai_browser - a tiny interactive OAI-PMH client, in the spirit of the
Perl `oai_browser.pl` that ships with HTTP::OAI.

The original is by **Tim Brody** (ECS, University of Southampton, 2005-2012)
and lives in the HTTP-OAI distribution:

    https://metacpan.org/release/HTTP-OAI/source/script/oai_browser.pl

That one drove `HTTP::OAI::Harvester` from a numbered menu of the six verbs.
This is a small **stdlib-only** reimplementation for exercising our own
provider: it prompts for a base URL, does an `Identify`, then offers the same
six verbs and prints each response (pretty-printed for reading; the wire
format is unchanged).

    python3 tools/oai_browser.py [baseURL]

Options:
  --skip-identify   don't Identify on connect
  --silent          print a size/count line instead of the response body
  --all             follow resumption tokens to the end (list verbs)
  --trace           echo each request URL
  -h, --help        this text

Dependency-light on purpose: `urllib` + `xml.etree` from the standard library
(no lxml), which is also the project's rule for anything reached over HTTP.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET

OAI_NS = "http://www.openarchives.org/OAI/2.0/"

MENU = """
Menu
----
1. GetRecord           2. Identify
3. ListIdentifiers     4. ListMetadataFormats
5. ListRecords         6. ListSets
q. Quit
"""


# --- transport -------------------------------------------------------------


def fetch(base_url: str, params: dict[str, str], trace: bool = False) -> str:
    url = base_url + "?" + urllib.parse.urlencode(params)
    if trace:
        print(f"-> {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "oai_browser (oai-provider test client)"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


def pretty(text: str) -> str:
    """Indent for reading only. It never leaves this process."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


def oai_error(text: str) -> tuple[str, str] | None:
    """(code, message) if the response is an OAI error, else None."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    err = root.find(f"{{{OAI_NS}}}error")
    if err is None:
        return None
    return (err.get("code", ""), (err.text or "").strip())


def resumption_token(text: str) -> str:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    # the token is a child of the verb element, not of <OAI-PMH>, so search
    # the whole tree rather than root.find()
    for tok in root.iter(f"{{{OAI_NS}}}resumptionToken"):
        return (tok.text or "").strip()
    return ""


# --- presenting a response -------------------------------------------------


def show(text: str, silent: bool, trace: bool) -> None:
    err = oai_error(text)
    if err is not None:
        print(f"!! OAI error: {err[0]}: {err[1]}")
        return
    if silent:
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            print(f"{len(text)} bytes (unparseable)")
            return
        records = sum(1 for tag in root.iter() if tag.tag.endswith("}record"))
        headers = sum(1 for tag in root.iter() if tag.tag.endswith("}header"))
        print(f"{len(text)} bytes, {headers} header(s), {records} record(s)")
        return
    print(pretty(text))


# --- prompting -------------------------------------------------------------


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"{label}{suffix}> ").strip()
    except EOFError:
        return default
    return got or default


def ask_list_args() -> dict[str, str]:
    """Optional from/until/set for the list verbs; blank means 'not sent'."""
    params: dict[str, str] = {}
    for name in ("from", "until", "set"):
        value = ask(f"{name} (blank to skip)")
        if value:
            params[name] = value
    return params


# --- the six verbs ---------------------------------------------------------


def request_for(choice: str) -> tuple[str, dict[str, str]] | None:
    """Build the query for a menu choice. None means 'unknown menu key'."""
    if choice == "2":
        return ("Identify", {"verb": "Identify"})
    if choice == "6":
        return ("ListSets", {"verb": "ListSets"})
    if choice == "4":
        params = {"verb": "ListMetadataFormats"}
        ident = ask("identifier (blank for all formats)")
        if ident:
            params["identifier"] = ident
        return ("ListMetadataFormats", params)
    if choice == "1":
        return (
            "GetRecord",
            {
                "verb": "GetRecord",
                "identifier": ask("identifier"),
                "metadataPrefix": ask("metadataPrefix", "oai_dc"),
            },
        )
    if choice in ("3", "5"):
        verb = "ListIdentifiers" if choice == "3" else "ListRecords"
        params = {"verb": verb, "metadataPrefix": ask("metadataPrefix", "oai_dc")}
        params.update(ask_list_args())
        return (verb, params)
    return None


def run(base_url: str, choice: str, opts: dict[str, bool]) -> None:
    built = request_for(choice)
    if built is None:
        return
    verb, params = built
    print(f"\n== {verb} ==")
    try:
        body = fetch(base_url, params, opts["trace"])
    except urllib.error.HTTPError as exc:
        print(f"!! HTTP {exc.code}: {exc.reason}")
        return
    except urllib.error.URLError as exc:
        print(f"!! could not reach {base_url}: {exc.reason}")
        return

    show(body, opts["silent"], opts["trace"])

    # Follow resumption tokens on the list verbs, when asked.
    while opts["all"] and verb in {"ListIdentifiers", "ListRecords"}:
        token = resumption_token(body)
        if not token:
            break
        try:
            body = fetch(
                base_url,
                {"verb": verb, "resumptionToken": token},
                opts["trace"],
            )
        except urllib.error.URLError as exc:
            print(f"!! {exc}")
            return
        print("\n-- next page --")
        show(body, opts["silent"], opts["trace"])


# --- shell -----------------------------------------------------------------


def parse_options(argv: list[str]) -> tuple[dict[str, bool], str | None, bool]:
    opts = {
        "silent": False,
        "all": False,
        "trace": False,
        "skip_identify": False,
    }
    base: str | None = None
    help_wanted = False
    for arg in argv:
        if arg in ("-h", "--help"):
            help_wanted = True
        elif arg == "--silent":
            opts["silent"] = True
        elif arg == "--all":
            opts["all"] = True
        elif arg == "--trace":
            opts["trace"] = True
        elif arg == "--skip-identify":
            opts["skip_identify"] = True
        elif arg.startswith("-"):
            print(f"unknown option: {arg}")
            help_wanted = True
        else:
            base = arg
    return opts, base, help_wanted


def main(argv: list[str]) -> int:
    opts, base, help_wanted = parse_options(argv)
    if help_wanted:
        print(__doc__)
        return 0

    try:  # arrow-key history, when the platform has readline
        import readline  # noqa: F401

        readline.add_history(base or "")
    except ImportError:
        pass

    print(
        "Welcome to the Open Archives Browser (oai-provider test client)\n"
        "In the spirit of oai_browser.pl by Tim Brody (HTTP-OAI).\n"
        "Use CTRL+C to quit at any time\n\n---"
    )

    if not base:
        base = ask("OAI Base URL to query", "http://localhost:8000/oai")
    print(f"baseURL: {base}")

    if not opts["skip_identify"]:
        print("\n== Identify ==")
        try:
            show(fetch(base, {"verb": "Identify"}, opts["trace"]), opts["silent"], opts["trace"])
        except urllib.error.URLError as exc:
            print(f"!! could not reach {base}: {exc.reason}")
            return 1

    while True:
        print(MENU)
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice in ("q", "quit", "exit"):
            break
        if choice == "?":
            print(__doc__)
            continue
        try:
            run(base, choice, opts)
        except KeyboardInterrupt:
            print()
        except Exception as exc:  # noqa: BLE001 - a browser must keep going
            print(f"!! internal error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
