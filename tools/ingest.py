#!/usr/bin/env python3
"""Ingest one big multi-record XML dump into BaseX for the OAI provider.

Three passes, because BaseX will not let one query both store and report:

  1. validate - read-only; shows what the mapping extracts, per record
  2. ingest   - updating; envelops and stores every record in the dump
  3. reconcile- updating; tombstones/removes records not seen in this dump

Pass 3 is the one the work setup lacks. The source does not record deletes,
so without it a vanished object stays live forever and harvesters never learn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oai.basex import BaseXClient, BaseXError  # noqa: E402
from oai.config import ENVELOPE_NS, Config, ConfigError  # noqa: E402
from oai.mapping import QueryBuilder  # noqa: E402

ENV = f"{{{ENVELOPE_NS}}}"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dump_id_for(path: Path, explicit: str | None) -> str:
    """Identify this dump run.

    Default is content-based, so re-running the same file is idempotent and
    reconcile cannot mistake an unchanged dump for a dump that lost records.
    """
    if explicit:
        return explicit
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"{path.name}:{digest}"


async def run(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    builder = QueryBuilder(config.mapping)
    dump = Path(args.dump)
    if not dump.exists():
        print(f"dump not found: {dump}", file=sys.stderr)
        return 2

    if config.modules:
        return await run_modules(args, config, builder)

    dump_id = dump_id_for(dump, args.dump_id)
    variables = {
        "path": str(dump.resolve()),
        "db": config.basex.database,
        "idPrefix": config.mapping.identifier_prefix,
        "tzOffset": config.mapping.timezone_offset,
        "dumpId": dump_id,
        "now": utcnow(),
        "policy": config.identity.deleted_record,
    }

    async with BaseXClient(
        config.basex.url,
        config.basex.user,
        config.basex.password,
        timeout=config.basex.timeout,
    ) as bx:
        if not await bx.ping():
            print("BaseX is not answering at " f"{config.basex.url}", file=sys.stderr)
            return 3

        if not config.basex.from_env:
            print(
                "warning: BaseX password came from the config file. "
                "Prefer OAI_BASEX_PASSWORD before this is exposed.",
                file=sys.stderr,
            )

        # --- pass 1: validate -------------------------------------------
        t0 = time.monotonic()
        report = await bx.query_xml(builder.validate_query(), **variables)
        if report is None:
            print("validate returned nothing - is the dump well-formed?")
            return 4
        records = int(report.get("records", "0"))
        storable = int(report.get("storable", "0"))
        missing = int(report.get("missing", "0"))
        print(
            f"validate: {records} record(s) at '{config.mapping.records}', "
            f"{storable} storable, {missing} missing id or datestamp "
            f"({time.monotonic() - t0:.1f}s)"
        )
        if args.verbose or args.dry_run:
            for row in list(report)[: args.limit]:
                flag = "ok " if row.get("storable") == "true" else "SKIP"
                print(
                    f"  {flag} {row.get('identifier')}"
                    f"  ds={row.get('datestamp')}"
                    f"  sets={row.get('sets') or '-'}"
                )
            if records > args.limit and not args.verbose:
                print(f"  ... {records - args.limit} more (--verbose for all)")
        if args.dry_run:
            print("dry run: nothing written")
            return 0
        if storable == 0:
            print(
                "refusing to reconcile: the dump yielded no storable records. "
                "A bad mapping would otherwise tombstone the whole database.",
                file=sys.stderr,
            )
            return 5

        # --- pass 2: ingest ---------------------------------------------
        t0 = time.monotonic()
        await bx.query(builder.ingest_query(), **variables)
        stored = await bx.count_records(config.basex.database, ENVELOPE_NS)
        print(
            f"ingest: {storable} record(s) written, {stored} total in "
            f"'{config.basex.database}' ({time.monotonic() - t0:.1f}s, dump id {dump_id[:24]})"
        )

        # --- pass 3: reconcile ------------------------------------------
        if args.no_reconcile:
            print("reconcile: skipped (--no-reconcile)")
            return 0
        t0 = time.monotonic()
        # Count first: reconcile is updating and cannot report its own effect,
        # and a document-count delta is meaningless under "persistent" because
        # tombstones keep their documents.
        stale = int(
            (
                await bx.query(
                    builder.stale_query(),
                    db=config.basex.database,
                    dumpId=dump_id,
                )
            ).strip()
            or 0
        )
        await bx.query(builder.reconcile_query(), **variables)
        policy = config.identity.deleted_record
        if policy == "no":
            print(
                f"reconcile: policy 'no' - {stale} record(s) not seen in this "
                "dump left alone (source owns liveness)"
            )
        else:
            verb = "tombstoned" if policy == "persistent" else "removed"
            print(
                f"reconcile: policy {policy!r}, {stale} record(s) not seen in "
                f"this dump, {verb} ({time.monotonic() - t0:.1f}s)"
            )
        return 0


async def run_modules(args: argparse.Namespace, config, builder) -> int:
    """Ingest every module into its own database.

    This is the layout that emulates the colleague's setup: one database per
    module (`sync_Object`, `sync_Person`, `sync_Multimedia`), namespaces
    stripped, records stored under

        application/modules/module[@name='<Name>']/moduleItem

    so his own queries read against our databases unchanged.

    **Additive by default.** Each record is `db:put` at `<Module>-<id>.xml`, so
    the id is the key: a new id is added, an existing id is replaced, and a
    record this dump does not mention is left alone. That makes importing
    several chunks of one export safe - which is the point, because the source
    records no deletes.

    `--reset` instead drops and rebuilds each module's database from this dump,
    which is what leaves a vanished record behind on the next run; it prints
    what it drops first. A module **absent** from the dump is skipped (its
    database untouched), so a per-module or partial dump merges cleanly; a
    module present but empty is still refused.
    """
    dump = Path(args.dump).resolve()
    path = str(dump)
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
            declared = report.get("declared", "")
            print(
                f"{module.name}: {items} record(s) "
                f"(server declared {declared or '?'}), {with_id} with an id "
                f"-> {module.database} ({time.monotonic() - t0:.1f}s)"
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
    parser.add_argument("--dry-run", action="store_true", help="validate only")
    parser.add_argument("--dump-id", default=None, help="override the dump identity")
    parser.add_argument("--no-reconcile", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="module mode: DROP each module database and rebuild it from this "
        "dump (destructive, and it prints what it drops). The default is "
        "additive: records are written in place, so a record this dump does "
        "not contain is left alone.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="deprecated: additive is now the default, so this does nothing",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
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
