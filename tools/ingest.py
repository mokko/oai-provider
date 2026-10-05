#!/usr/bin/env python3
"""Ingest MuseumPlus XML dumps - or the zips they arrive in - into BaseX.

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

**Several dumps are accepted, and a directory.** An export arrives as dozens of
chunk files: naming the directory ingests every one of them in **chunk order**,
and the batch **stops at the first dump it cannot read**, rather than finishing
a run whose data is quietly missing a chapter.

**A `.zip` is unpacked here, because BaseX cannot do it.** BaseX reads zips
perfectly well - its `archive:` module lists a deflated archive and parses the
entry out of it - but these chunks are **LZMA** compressed (`method 14`), and
the JDK zip reader BaseX uses handles stored and deflated only:

    [archive:error] invalid CEN header (bad compression method: 14)

`zipfile` and `lzma` are both in the standard library, so the archive is
unpacked to a temporary file beside it and **deleted again** when that dump is
done: the archive is the artefact that is kept, the unpacked XML is only the
means.

**A receipt says what is already in the store.** Each dump that goes in is
recorded beside its own chunks, in `.ingest-receipt.json` (hidden, so a directory
scan never takes it for a dump): the file's size and mtime, when it was ingested,
and the databases' document counts afterwards. A later run **skips a dump the
receipt names with the same size and mtime** — which is what makes an interrupted
45-minute import resumable instead of restartable. The receipt is a cache and not
truth: if a database holds fewer documents than the receipt records, it is ignored
for that run and said out loud, because a receipt that lies would skip chunks and
leave a hole the size of a chapter. `--force` ignores it on purpose.

BaseX will not let one query both store and report, so a read-only count pass runs
before the write pass — **once for all the modules, not once per module**:
`parse-xml` reads and parses the whole 135-500 MB file, so a per-module count paid
that cost per module (6.1 s each on a 135 MB chunk, for a 44-record module as much
as for a 1000-record one). A module **absent** from the dump is skipped (its
database untouched), so a per-module or partial dump merges cleanly; a module
present but empty is still refused.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import lzma
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oai.basex import BaseXClient, BaseXError  # noqa: E402
from oai.config import Config, ConfigError  # noqa: E402
from oai.mapping import QueryBuilder  # noqa: E402

## What a dump may be. Anything else in a named directory is left alone.
SUFFIXES = (".xml", ".zip")

## The receipt's file name. Hidden, so `collect()`'s scan of the same directory
## never takes it for a chunk.
RECEIPT = ".ingest-receipt.json"


class DumpError(Exception):
    """A dump that cannot be read. An error of its own because a batch stops on it."""


def chunk_key(path: Path) -> list[object]:
    """Sort key that orders `chunk2` before `chunk10`.

    Sorting names as strings puts chunk10 between chunk1 and chunk2, which is
    cosmetic in a listing and wrong in an ingest: these numbers are the order of
    the export.
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def collect(sources: list[str]) -> list[Path]:
    """Every dump to ingest, in the order they are to be ingested.

    A **directory** contributes its `.xml` and `.zip` in chunk order. Where one
    chunk is present twice - the archive as it arrived, and the `.xml` somebody
    unpacked by hand before this CLI could - the **archive wins**: the archive is
    what is kept, and the file beside it is a leftover of doing by hand what
    `unpacked()` now does itself.
    """
    dumps: dict[str, Path] = {}
    for source in sources:
        named = Path(source)
        if not named.is_dir():
            # Named on the command line: taken as itself, whatever it is called.
            dumps[named.stem] = named
            continue
        for child in sorted(named.iterdir(), key=chunk_key):
            if child.suffix.lower() not in SUFFIXES:
                continue
            if child.name.startswith("."):
                # Hidden files are never dumps: `unpacked()` writes its temporary
                # copy with a leading dot precisely so a later scan of the same
                # directory cannot mistake a leftover for a chunk.
                continue
            known = dumps.get(child.stem)
            if known is None or child.suffix.lower() == ".zip":
                dumps[child.stem] = child
    return list(dumps.values())


@contextlib.contextmanager
def unpacked(dump: Path) -> Iterator[Path]:
    """The file to hand BaseX for `dump`: itself, or a temporary unpacked copy.

    See the module docstring for why the unpacking happens here and not in the
    query. The temporary file is written **beside the archive, not in
    `TMPDIR`**: `/tmp` is a tmpfs on the machine this is developed on, and one
    chunk unpacks to up to ~0.5 GB, which is RAM there rather than disk.
    """
    if dump.suffix.lower() != ".zip":
        yield dump
        return

    try:
        with zipfile.ZipFile(dump) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if len(members) != 1:
                raise DumpError(
                    f"{dump.name}: expected one .xml inside the archive, found "
                    f"{len(members)} ({', '.join(members) or 'none'})"
                )
            handle = tempfile.NamedTemporaryFile(
                dir=dump.parent, prefix=f".{dump.name}.", suffix=".tmp.xml", delete=False
            )
            try:
                with archive.open(members[0]) as packed, handle:
                    shutil.copyfileobj(packed, handle, 1024 * 1024)
            except BaseException:
                # Nothing of a half-unpacked dump is left behind, whatever went
                # wrong - including a Ctrl-C during a 0.5 GB chunk.
                handle.close()
                Path(handle.name).unlink(missing_ok=True)
                raise
    except (zipfile.BadZipFile, lzma.LZMAError, NotImplementedError) as exc:
        raise DumpError(f"{dump.name}: cannot be unpacked ({exc})") from exc

    temporary = Path(handle.name)
    try:
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def read_receipt(directory: Path) -> dict:
    """The receipt as written: `{"databases": [...], "dumps": {...}}`.

    Empty when there is none, **and empty when it cannot be read**: the receipt
    is a cache of what has been done, so a corrupt one must cost a re-ingest and
    never a failed run.
    """
    path = directory / RECEIPT
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: {path} is not readable ({exc}) - ignoring it", file=sys.stderr)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("dumps"), dict):
        print(f"warning: {path} is not a receipt this understands - ignoring it", file=sys.stderr)
        return {}
    return data


def receipt_dumps(receipt: dict, databases: list[str]) -> dict:
    """The receipt's dump records, **if it was written for these databases**.

    A receipt names the databases it describes. One written for another store — a
    probe, a second config, an install rebuilt somewhere else — records counts for
    databases this run knows nothing about, and its "already ingested" would be
    about records that are not here. Empty means "nothing to skip", which is the
    safe direction: the work is done again rather than skipped wrongly.
    """
    if sorted(str(name) for name in receipt.get("databases", [])) != sorted(databases):
        return {}
    return dict(receipt.get("dumps", {}))


def write_receipt(directory: Path, databases: list[str], dumps: dict) -> None:
    """Write the receipt **atomically**.

    A half-written receipt is read back as "this chunk was ingested" for a chunk
    that was not, and that is a hole in the store with nothing to show for it, so
    the file is written beside itself and moved into place.
    """
    path = directory / RECEIPT
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(
        json.dumps({"databases": sorted(databases), "dumps": dumps}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def already_ingested(receipt: dict, dump: Path) -> dict | None:
    """The receipt's record for `dump`, **if it is the same file**.

    Size and mtime are the test, not the name: a chunk delivered again has both,
    while a file that was re-exported, replaced or truncated has not - and a name
    alone would be a lie the moment a chunk is rebuilt under the same name.
    """
    record = receipt.get(dump.name)
    if not isinstance(record, dict):
        return None
    stat = dump.stat()
    if record.get("size") != stat.st_size or record.get("modified") != int(stat.st_mtime):
        return None
    return record


def receipt_is_trustworthy(receipt: dict, live: dict[str, int]) -> tuple[bool, str]:
    """Whether the receipt still describes this store, and why not if it does not.

    Each entry records how many documents every database held *after* that dump.
    If a database now holds fewer than the highest figure the receipt recalls,
    something dropped data behind the receipt's back, and honouring it would skip
    chunks and leave a hole - so the whole receipt is ignored for the run and the
    dumps are re-ingested. Checked once per run: a few counts, not one per chunk.
    """
    for database, count in live.items():
        recorded = max(
            (
                int(entry.get("documents", {}).get(database, 0))
                for entry in receipt.values()
                if isinstance(entry, dict)
            ),
            default=0,
        )
        if count < recorded:
            return False, (
                f"{database} holds {count} document(s) but the receipt records "
                f"{recorded}: something dropped data since, so the receipt is "
                "ignored and the dumps are ingested again"
            )
    return True, ""


async def count_modules(
    bx: BaseXClient, builder: QueryBuilder, config: Config, path: str
) -> dict[str, dict[str, str]] | None:
    """The count pass for one dump: every module, in a single query.

    One parse of the file rather than one per module - see the module docstring.
    None means the query answered nothing at all.
    """
    started = time.monotonic()
    report = await bx.query_xml(
        builder.module_counts_query(),
        path=path,
        moduleNames=",".join(module.name for module in config.modules),
    )
    if report is None:
        print("the count pass answered nothing", file=sys.stderr)
        return None
    counts = {child.get("name", ""): dict(child.attrib) for child in report.findall("module")}
    print(f"count pass: {len(counts)} module(s) in {time.monotonic() - started:.1f}s")
    return counts


async def ingest_one(
    bx: BaseXClient,
    config: Config,
    builder: QueryBuilder,
    path: str,
    args: argparse.Namespace,
    sizes: dict[str, int],
    counts: dict[str, dict[str, str]],
) -> int:
    """One dump: the write pass, module by module, against the count pass already run.

    `sizes` collects each module database's document count as it is written, so
    the batch can say what the databases hold at the end. That is deliberately not
    a running total: `count_documents` is the whole database every time, so adding
    it up per dump would count the first chunk 48 times.
    """
    for module in config.modules:
        report = counts.get(module.name)
        if report is None:
            # The count pass named every configured module; one missing means the
            # report itself is not what this expects, not that the module is
            # absent (which says present="false").
            print(f"{module.name}: the count pass did not report it", file=sys.stderr)
            return 4
        present = report.get("present") == "true"
        items = int(report.get("items", "0"))
        with_id = int(report.get("withId", "0"))
        undated = int(report.get("undated", "0"))
        declared = report.get("declared", "")
        print(
            f"{module.name}: {items} record(s) "
            f"(server declared {declared or '?'}), {with_id} with an id "
            f"-> {module.database}"
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
        sizes[module.database] = stored
        print(
            f"  ingest: {stored} document(s) in '{module.database}' "
            f"({time.monotonic() - t1:.1f}s)"
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    builder = QueryBuilder(config.modules, config.timezone_offset)

    dumps = collect(args.dumps)
    if not dumps:
        print("no dumps to ingest", file=sys.stderr)
        return 2
    for dump in dumps:
        if not dump.exists():
            print(f"dump not found: {dump}", file=sys.stderr)
            return 2
    # `--reset` rebuilds a database from ONE dump, so a batch would drop and
    # rebuild once per chunk and end with only the last chunk's records - a
    # database that looks perfectly fine. Refused rather than warned about,
    # because the two flags are easy to combine by accident.
    if args.reset and len(dumps) > 1:
        print(
            f"--reset rebuilds a module database from a single dump, so it is "
            f"refused for the {len(dumps)} dumps given (it would leave the last "
            "one as the only data). The default is additive; or name one dump.",
            file=sys.stderr,
        )
        return 2
    if not config.modules:
        print(
            "no [[modules]] configured, and module mode is the only ingest "
            "path: nothing to write into.",
            file=sys.stderr,
        )
        return 2

    sizes: dict[str, int] = {}
    skipped = 0
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

        # One receipt per directory the dumps came from: it travels with the
        # chunks it names and says nothing about a directory it never saw. Read
        # scoped to **these** databases, so a receipt written for another store
        # cannot make this run skip a chunk it has never ingested.
        databases = [module.database for module in config.modules]
        receipts: dict[Path, dict] = {}
        for dump in dumps:
            if dump.parent in receipts:
                continue
            written = read_receipt(dump.parent)
            scoped = receipt_dumps(written, databases)
            if written and not scoped:
                print(
                    f"warning: {dump.parent / RECEIPT} describes other databases "
                    f"({', '.join(str(n) for n in written.get('databases', [])) or '?'}) "
                    "- ignored",
                    file=sys.stderr,
                )
            receipts[dump.parent] = scoped
        if args.reset:
            # `--reset` drops each module database and rebuilds it from this one
            # dump, so every other entry in the receipt describes records that
            # are no longer there: the receipt starts over. (The trust check
            # below would catch it on the next run anyway - saying it here is
            # cheaper than re-ingesting 47 chunks to find out.)
            for directory in receipts:
                receipts[directory] = {}
        if not args.force and any(receipts.values()):
            live: dict[str, int] = {}
            for module in config.modules:
                live[module.database] = (
                    await bx.count_documents(module.database)
                    if await bx.database_exists(module.database)
                    else 0
                )
            for directory, receipt in receipts.items():
                if not receipt:
                    continue
                trustworthy, why = receipt_is_trustworthy(receipt, live)
                if not trustworthy:
                    print(f"warning: {directory / RECEIPT}: {why}", file=sys.stderr)
                    # Started over rather than trusted: the received chunks in
                    # this run are re-ingested, and the receipt is rebuilt from
                    # what this run actually does.
                    receipts[directory] = {}

        for number, dump in enumerate(dumps, start=1):
            directory = dump.parent
            receipt = receipts.setdefault(directory, {})
            if len(dumps) > 1:
                print(f"[{number}/{len(dumps)}] {dump.name}")
            if not args.force:
                known = already_ingested(receipt, dump)
                if known is not None:
                    print(
                        f"  already in the store (ingested {known.get('ingested', '?')}), "
                        "skipped"
                    )
                    skipped += 1
                    continue
            with unpacked(dump) as path:
                resolved = str(path.resolve())
                counts = await count_modules(bx, builder, config, resolved)
                if counts is None:
                    return 4
                code = await ingest_one(bx, config, builder, resolved, args, sizes, counts)
            if code != 0:
                # Stop at the first dump that failed: finishing the batch would
                # write the rest and leave a store that is missing a chapter,
                # with nothing on screen to say which one.
                print(f"stopped at {dump.name}: nothing after it was read", file=sys.stderr)
                return code
            if not args.dry_run:
                # Written after each dump, not at the end: that is the whole point
                # of a receipt - an interrupted run resumes where it stopped.
                stat = dump.stat()
                receipt[dump.name] = {
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                    "ingested": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "documents": dict(sizes),
                }
                write_receipt(directory, databases, receipt)

    if args.dry_run:
        print(f"dry run: nothing written ({len(dumps)} dump(s) counted)")
    elif sizes:
        tail = f", {skipped} already in the store" if skipped else ""
        print(f"done: {len(dumps)} dump(s), additive{tail}. Stored now:")
        for database, count in sizes.items():
            print(f"  {database}: {count} document(s)")
    else:
        print(f"done: {len(dumps)} dump(s), nothing written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "dumps",
        nargs="+",
        help="what to ingest: an .xml file, a .zip holding one, or a directory "
        "of either. A directory is read in chunk order, and the batch stops at "
        "the first dump that cannot be read.",
    )
    parser.add_argument("-c", "--config", default="oai.toml")
    parser.add_argument("--dry-run", action="store_true", help="count only")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ingest every dump even if the receipt beside it says it is already "
        "in the store. The receipt is still updated: it is the record of what "
        "has been done, not a switch.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DROP each module database and rebuild it from this dump "
        "(destructive, and it prints what it drops). The default is additive: "
        "records are written in place, so a record this dump does not contain "
        "is left alone. Refused for more than one dump - it is a single-dump "
        "operation.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except (ConfigError, BaseXError, DumpError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
