"""What the ingest CLI reads, and how it unpacks a chunk.

No BaseX: this is the part that decides *which* file each dump is and hands
BaseX a path it can read. The zip handling is here because BaseX cannot unpack
these archives at all - the chunks are **LZMA** (`method 14`), which the JDK zip
reader BaseX uses refuses:

    [archive:error] invalid CEN header (bad compression method: 14)

`zipfile` reads them from the standard library, so the CLI unpacks each archive
to a temporary file beside it and deletes it again.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("ingest", ROOT / "tools" / "ingest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingest = _load()

PAYLOAD = b"<?xml version='1.0'?><application><modules/></application>"


def _zip(path: Path, member: str, payload: bytes, method: int = zipfile.ZIP_LZMA) -> Path:
    with zipfile.ZipFile(path, "w", method) as archive:
        archive.writestr(member, payload)
    return path


# -- what gets ingested --------------------------------------------------


def test_a_directory_is_read_in_chunk_order(tmp_path: Path) -> None:
    for number in (10, 2, 1, 20):
        (tmp_path / f"query1035073-chunk{number}.xml").write_text("<application/>")
    assert [p.name for p in ingest.collect([str(tmp_path)])] == [
        "query1035073-chunk1.xml",
        "query1035073-chunk2.xml",
        "query1035073-chunk10.xml",
        "query1035073-chunk20.xml",
    ]


def test_the_archive_wins_over_the_file_unpacked_beside_it(tmp_path: Path) -> None:
    (tmp_path / "query1035073-chunk2.xml").write_text("<application/>")
    _zip(tmp_path / "query1035073-chunk2.zip", "query1035073-chunk2.xml", PAYLOAD)
    assert [p.name for p in ingest.collect([str(tmp_path)])] == [
        "query1035073-chunk2.zip"
    ]


def test_anything_else_in_the_directory_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("not a dump")
    (tmp_path / "Dump.xml.bak").write_text("nor this")
    # A leftover of a run that was killed mid-unpack: hidden, so never a chunk.
    (tmp_path / ".query1035073-chunk2.zip.abc123.tmp.xml").write_text("<application/>")
    assert ingest.collect([str(tmp_path)]) == []


def test_a_named_file_is_taken_as_itself(tmp_path: Path) -> None:
    odd = tmp_path / "ria-dump"
    odd.write_text("<application/>")
    assert [p.name for p in ingest.collect([str(odd)])] == ["ria-dump"]


def test_named_files_keep_the_order_they_were_given_in(tmp_path: Path) -> None:
    first = tmp_path / "b.xml"
    second = tmp_path / "a.xml"
    first.write_text("<application/>")
    second.write_text("<application/>")
    assert [p.name for p in ingest.collect([str(first), str(second)])] == ["b.xml", "a.xml"]


# -- unpacking -----------------------------------------------------------


def test_a_plain_xml_is_handed_over_untouched(tmp_path: Path) -> None:
    dump = tmp_path / "Dump.xml"
    dump.write_text("<application/>")
    with ingest.unpacked(dump) as path:
        assert path == dump


def test_an_lzma_archive_is_unpacked_beside_it_and_deleted_again(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "query1035073-chunk2.zip", "query1035073-chunk2.xml", PAYLOAD)
    with ingest.unpacked(archive) as path:
        assert path != archive
        assert path.parent == tmp_path, "beside the archive, not in TMPDIR"
        assert path.name.startswith("."), "hidden, so a scan cannot take it for a dump"
        assert path.read_bytes() == PAYLOAD
    assert not path.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["query1035073-chunk2.zip"]


def test_a_deflated_archive_works_the_same(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "query1035073-chunk3.zip",
        "query1035073-chunk3.xml",
        PAYLOAD,
        zipfile.ZIP_DEFLATED,
    )
    with ingest.unpacked(archive) as path:
        assert path.read_bytes() == PAYLOAD
    assert not path.exists()


def test_the_unpacked_copy_goes_even_when_the_ingest_fails(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "query1035073-chunk4.zip", "query1035073-chunk4.xml", PAYLOAD)
    with pytest.raises(RuntimeError):
        with ingest.unpacked(archive) as path:
            raise RuntimeError("BaseX blew up halfway through")
    assert not path.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["query1035073-chunk4.zip"]


# -- archives that cannot be used ----------------------------------------


def test_an_archive_with_no_xml_inside_is_refused(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "chunk5.zip", "readme.txt", b"no records here")
    with pytest.raises(ingest.DumpError, match="expected one .xml"):
        with ingest.unpacked(archive):
            pass


def test_an_archive_with_two_xml_files_is_refused(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "chunk6.zip", "w", zipfile.ZIP_LZMA) as archive:
        archive.writestr("a.xml", PAYLOAD)
        archive.writestr("b.xml", PAYLOAD)
    with pytest.raises(ingest.DumpError, match="found 2"):
        with ingest.unpacked(tmp_path / "chunk6.zip"):
            pass


def test_a_file_that_is_not_an_archive_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "chunk7.zip"
    broken.write_bytes(b"this is not a zip")
    with pytest.raises(ingest.DumpError, match="cannot be unpacked"):
        with ingest.unpacked(broken):
            pass


# -- the CLI, before it reaches BaseX ------------------------------------


def test_reset_is_refused_for_more_than_one_dump(tmp_path: Path, monkeypatch) -> None:
    for number in (1, 2):
        (tmp_path / f"query1035073-chunk{number}.xml").write_text("<application/>")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest.py", "-c", str(ROOT / "oai.toml"), "--reset", str(tmp_path)],
    )
    assert ingest.main() == 2


def test_a_missing_dump_is_refused_before_anything_is_connected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest.py", "-c", str(ROOT / "oai.toml"), str(tmp_path / "nope.xml")],
    )
    assert ingest.main() == 2
