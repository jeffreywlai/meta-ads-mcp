"""A tool may read a local file only from the folder the operator named, and only a real file there.

Tools that upload a local file send its contents to Meta. The calling model also
reads text written by other people (ad comments, Page reviews), which can carry
instructions. So a file path from the model must not be able to reach anything
outside one folder the operator chose.
"""

from __future__ import annotations

import os

import pytest

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.local_files import read_upload_file

TYPES = {".csv": "text/csv", ".zip": "application/zip"}


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    folder = tmp_path / "uploads"
    folder.mkdir()
    monkeypatch.setenv("META_UPLOAD_DIR", str(folder))
    reload_settings()
    return folder


def read(path, **overrides):
    options = {"allowed_types": TYPES, "max_bytes": 1_000, "parameter": "file_path"}
    options.update(overrides)
    return read_upload_file(path, **options)


def test_reads_a_file_inside_the_upload_folder(upload_dir) -> None:
    (upload_dir / "feed.csv").write_bytes(b"id\n1\n")
    result = read(str(upload_dir / "feed.csv"))
    assert (result.name, result.content, result.content_type) == ("feed.csv", b"id\n1\n", "text/csv")


def test_a_relative_path_is_taken_from_the_upload_folder(upload_dir) -> None:
    (upload_dir / "sub").mkdir()
    (upload_dir / "sub" / "Feed.CSV").write_bytes(b"x")
    assert read("sub/Feed.CSV").content == b"x"


def test_reading_is_off_until_a_folder_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("META_UPLOAD_DIR", raising=False)
    reload_settings()
    target = tmp_path / "feed.csv"
    target.write_bytes(b"x")
    with pytest.raises(ValidationError, match="META_UPLOAD_DIR"):
        read(str(target))


def test_a_configured_folder_that_does_not_exist_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("META_UPLOAD_DIR", str(tmp_path / "missing"))
    reload_settings()
    with pytest.raises(ValidationError, match="META_UPLOAD_DIR"):
        read("feed.csv")


@pytest.mark.parametrize("escape", ["../secret.csv", "sub/../../secret.csv"])
def test_a_path_that_climbs_out_of_the_folder_is_refused(upload_dir, escape) -> None:
    (upload_dir / "sub").mkdir()
    (upload_dir.parent / "secret.csv").write_bytes(b"secret")
    with pytest.raises(ValidationError, match="inside the upload folder"):
        read(escape)


def test_an_absolute_path_outside_the_folder_is_refused(upload_dir, tmp_path) -> None:
    outside = tmp_path / "elsewhere.csv"
    outside.write_bytes(b"secret")
    with pytest.raises(ValidationError, match="inside the upload folder"):
        read(str(outside))


def test_a_link_inside_the_folder_to_a_file_outside_is_refused(upload_dir, tmp_path) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    (upload_dir / "feed.csv").symlink_to(secret)
    with pytest.raises(ValidationError, match="inside the upload folder"):
        read("feed.csv")


def test_a_link_to_a_wrong_type_inside_the_folder_is_refused(upload_dir) -> None:
    (upload_dir / "notes.txt").write_bytes(b"not a feed")
    (upload_dir / "feed.csv").symlink_to(upload_dir / "notes.txt")
    with pytest.raises(ValidationError, match="must end in"):
        read("feed.csv")


def test_a_linked_folder_inside_the_upload_folder_cannot_reach_outside(upload_dir, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "feed.csv").write_bytes(b"secret")
    (upload_dir / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError, match="inside the upload folder"):
        read("link/feed.csv")


@pytest.mark.parametrize("name", ["feed.txt", "feed", "feed.csv.exe", ".csv"])
def test_other_file_types_are_refused_listing_the_allowed_ones(upload_dir, name) -> None:
    (upload_dir / name).write_bytes(b"x")
    with pytest.raises(ValidationError, match=r"\.csv, \.zip"):
        read(name)


def test_missing_empty_oversized_and_folder_paths_are_refused(upload_dir) -> None:
    with pytest.raises(ValidationError, match="does not point to a file"):
        read("missing.csv")
    (upload_dir / "empty.csv").write_bytes(b"")
    with pytest.raises(ValidationError, match="empty"):
        read("empty.csv")
    (upload_dir / "big.csv").write_bytes(b"x" * 1_001)
    with pytest.raises(ValidationError, match="1000-byte limit"):
        read("big.csv")
    (upload_dir / "folder.csv").mkdir()
    with pytest.raises(ValidationError, match="does not point to a file"):
        read("folder.csv")


@pytest.mark.parametrize("bad", [None, "", "   ", 5, ["feed.csv"], "feed\x00.csv"])
def test_values_that_are_not_a_path_are_refused(upload_dir, bad) -> None:
    with pytest.raises(ValidationError):
        read(bad)


def test_errors_never_quote_the_path_or_the_folder(upload_dir, tmp_path) -> None:
    outside = tmp_path / "customer-list-for-acme.csv"
    outside.write_bytes(b"secret")
    for attempt in (str(outside), "../customer-list-for-acme.csv", "no-such-customer-file.csv"):
        with pytest.raises(ValidationError) as error:
            read(attempt)
        message = str(error.value)
        assert "customer" not in message and "acme" not in message
        assert str(tmp_path) not in message
        assert error.value.__cause__ is None


def test_home_shorthand_is_expanded_before_the_check(upload_dir, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "home.csv").write_bytes(b"outside")
    with pytest.raises(ValidationError, match="inside the upload folder"):
        read("~/home.csv")
    monkeypatch.setenv("META_UPLOAD_DIR", "~/uploads")
    reload_settings()
    (upload_dir / "ok.csv").write_bytes(b"ok")
    assert read("~/uploads/ok.csv").content == b"ok"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_is_refused_without_blocking(upload_dir) -> None:
    os.mkfifo(upload_dir / "pipe.csv")
    with pytest.raises(ValidationError, match="does not point to a file"):
        read("pipe.csv")
