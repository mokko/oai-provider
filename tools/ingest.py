#!/usr/bin/env python3
"""Ingest one big multi-record XML dump into BaseX for the OAI provider.

One database per MuseumPlus module (`sync_Object`, `sync_Person`,
`sync_Multimedia`), the module's records stored namespace-stripped, one
document each:

    application/modules/module[@name='<Name>']/moduleItem

which is the layout the colleague's own `sync_*` databases use, so a query
written for his setup reads identically against ours.

**Additive by default.** Each record is written at `<Module>-<id>.xml`, so the
id is the key: a new id is added, an existing id is replaced, and a record this
dump does not mention is left alone. That is what makes importing several
chunks of one export safe - which matters, because the source records no
deletes. `--reset` instead drops a module's database and rebuilds it from this
dump, printing what it drops first.

BaseX will not let one query both store and report, so a read-only count pass
runs before each write pass. A module **absent** from the dump is skipped (its
database untouched), so a per-module or partial dump merges cleanly; a module
present but empty is still refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oai.basex import BaseXClient, BaseXError  # noqa: E402
from oai.config import Config, ConfigError  # noqa: E402
from oai.mapping import QueryBuilder  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    builder = QueryBuilder(config.modules, config.timezone_offset)
    dump = Path(args.dump)
    if not dump.exists():
        print(f"dump not found: {dump}", file=sys.stderr)
        return 2
    if not config.modules:
        print(
            "no [[modules]] configured, and module mode is the only ingest "
            "path: nothing to write into.",
            file=sys.stderr,
        )
        return 2

    path = str(dump.resolve())
    written = 0
    async with BaseXClient(
        config.basex.url,
        config.basex.user,
        config.basex.password,
        timeout=config.basex.timeout,
    ) as bx:
        if not await bx.ping():
            print(f"BaseX is not answering at {config.basex.url}", file=sys.stderr)
            return 3
        if not config.basex.from_env:
            print(
                "warning: BaseX password came from the config file. "
                "Prefer OAI_BASEX_PASSWORD before this is exposed.",
                file=sys.stderr,
            )

        for module in config.modules:
            t0 = time.monotonic()
            report = await bx.query_xml(
                builder.module_count_query(), path=path, moduleName=module.name
            )
            if report is None:
                print(f"{module.name}: no report from the dump", file=sys.stderr)
                return 4
            present = report.get("present") == "true"
            items = int(report.get("items", "0"))
            with_id = int(report.get("withId", "0"))
            undated = int(report.get("undated", "0"))
            declared = report.get("declared", "")
            print(
                f"{module.name}: {items} record(s) "
                f"(server declared {declared or '?'}), {with_id} with an id "
                f"-> {module.database} ({time.monotonic() - t0:.1f}s)"
            )
            if undated:
                print(
                    f"  warning: {undated} {module.name} record(s) have no usable "
                    "__lastModified and will NOT be served - OAI requires a "
                    "datestamp, and the serve query drops what has none",
                    file=sys.stderr,
                )
            # A module this dump does not carry is not an error: leave its
            # database alone and carry on with the modules that are here.
            if not present:
                print(
                    f"  {module.name} is not in this dump - skipped, "
                    f"'{module.database}' untouched"
                )
                continue
            if args.dry_run:
                continue
            if items == 0:
                print(
                    f"refusing to write an empty {module.name} module "
                    "(a bad module name would otherwise drop the whole database)",
                    file=sys.stderr,
                )
                return 5

            if args.reset:
                if await bx.database_exists(module.database):
                    n = await bx.count_documents(module.database)
                    print(
                        f"  --reset: dropping '{module.database}' "
                        f"({n} document(s)) and rebuilding from this dump"
                    )
                    await bx.drop_database(module.database)
            if not await bx.database_exists(module.database):
                await bx.create_database(module.database)

            t1 = time.monotonic()
            await bx.query(
                builder.module_ingest_query(),
                path=path,
                db=module.database,
                moduleName=module.name,
            )
            stored = await bx.count_documents(module.database)
            written += stored
            print(
                f"  ingest: {stored} document(s) in '{module.database}' "
                f"({time.monotonic() - t1:.1f}s)"
            )

    if args.dry_run:
        print("dry run: nothing written")
    else:
        print(f"done: {written} document(s) across {len(config.modules)} database(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dump", help="the big multi-record XML file")
    parser.add_argument("-c", "--config", default="oai.toml")
    parser.add_argument("--dry-run", action="store_true", help="count only")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DROP each module database and rebuild it from this dump "
        "(destructive, and it prints what it drops). The default is additive: "
        "records are written in place, so a record this dump does not contain "
        "is left alone.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except (ConfigError, BaseXError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
