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


def test_the_size_message_can_carry_advice_for_the_caller(upload_dir) -> None:
    (upload_dir / "big.csv").write_bytes(b"x" * 1_001)
    with pytest.raises(ValidationError, match="1000-byte limit for this upload. Host it instead."):
        read("big.csv", too_large_hint="Host it instead.")


# A folder on the checked path can be replaced with a link between the check and
# the read. Each test below makes that replacement at a chosen moment, through the
# module's own os.open, and the read must then either fail or return the file
# that really is inside the upload folder.


def _nested_layout(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    nested = root / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (nested / "feed.csv").write_bytes(b"inside-upload-folder")
    (outside / "feed.csv").write_bytes(b"synthetic-outside-secret")
    (outside / "nested" / "feed.csv").write_bytes(b"synthetic-outside-secret")
    monkeypatch.setenv("META_UPLOAD_DIR", str(root))
    reload_settings()
    return root, nested, outside


def _count_opens(monkeypatch, path):
    import meta_ads_mcp.local_files as local_files

    calls = []
    original = local_files.os.open

    def counting(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(local_files.os, "open", counting)
    read(path)
    monkeypatch.setattr(local_files.os, "open", original)
    return len(calls)


def _swap_before_open(monkeypatch, moment, swap):
    import meta_ads_mcp.local_files as local_files

    original = local_files.os.open
    seen = 0

    def swapping(*args, **kwargs):
        nonlocal seen
        if seen == moment:
            swap()
        seen += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(local_files.os, "open", swapping)


def _read_inside_or_refused(path):
    try:
        uploaded = read(path)
    except ValidationError as error:
        assert "synthetic" not in str(error) and error.__cause__ is None
        return None
    assert uploaded.content == b"inside-upload-folder", uploaded
    return uploaded


def test_g1_directory_swap_cannot_escape_upload_folder(tmp_path, monkeypatch) -> None:
    root, nested, outside = _nested_layout(tmp_path, monkeypatch)

    def swap():
        nested.rename(root / "saved-nested")
        nested.symlink_to(outside, target_is_directory=True)

    # The first open after the path was checked: the old code opened the full
    # path here and read the outside file.
    _swap_before_open(monkeypatch, 0, swap)
    assert _read_inside_or_refused("nested/feed.csv") is None


def test_a_parent_folder_swapped_at_any_moment_cannot_escape(tmp_path, monkeypatch) -> None:
    probe_root, _, _ = _nested_layout(tmp_path / "probe", monkeypatch)
    moments = _count_opens(monkeypatch, "nested/feed.csv")
    assert moments >= 2
    for moment in range(moments):
        base = tmp_path / f"moment-{moment}"
        root, nested, outside = _nested_layout(base, monkeypatch)

        def swap(root=root, nested=nested, outside=outside):
            nested.rename(root / "saved-nested")
            nested.symlink_to(outside, target_is_directory=True)

        _swap_before_open(monkeypatch, moment, swap)
        _read_inside_or_refused("nested/feed.csv")


def test_the_upload_folder_itself_swapped_for_a_link_cannot_escape(tmp_path, monkeypatch) -> None:
    _nested_layout(tmp_path / "probe", monkeypatch)
    moments = _count_opens(monkeypatch, "nested/feed.csv")
    for moment in range(moments):
        base = tmp_path / f"moment-{moment}"
        root, _, outside = _nested_layout(base, monkeypatch)

        def swap(root=root, outside=outside, base=base):
            root.rename(base / "saved-uploads")
            root.symlink_to(outside, target_is_directory=True)

        _swap_before_open(monkeypatch, moment, swap)
        _read_inside_or_refused("nested/feed.csv")


def test_a_file_swapped_for_a_link_at_the_last_moment_is_refused(tmp_path, monkeypatch) -> None:
    root, nested, outside = _nested_layout(tmp_path, monkeypatch)
    moments = _count_opens(monkeypatch, "nested/feed.csv")

    def swap():
        (nested / "feed.csv").unlink()
        (nested / "feed.csv").symlink_to(outside / "feed.csv")

    _swap_before_open(monkeypatch, moments - 1, swap)
    assert _read_inside_or_refused("nested/feed.csv") is None


def _track_descriptors(monkeypatch):
    import meta_ads_mcp.local_files as local_files

    opened = []
    original = local_files.os.open

    def tracking(*args, **kwargs):
        descriptor = original(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(local_files.os, "open", tracking)
    return opened


def _assert_all_closed(descriptors):
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_every_descriptor_is_closed_after_a_read(upload_dir, monkeypatch) -> None:
    (upload_dir / "sub").mkdir()
    (upload_dir / "sub" / "feed.csv").write_bytes(b"x")
    opened = _track_descriptors(monkeypatch)
    assert read("sub/feed.csv").content == b"x"
    _assert_all_closed(opened)


@pytest.mark.parametrize("case", ["empty", "oversized", "folder", "swapped"])
def test_every_descriptor_is_closed_after_a_refusal(upload_dir, tmp_path, monkeypatch, case) -> None:
    sub = upload_dir / "sub"
    sub.mkdir()
    if case == "empty":
        (sub / "feed.csv").write_bytes(b"")
    elif case == "oversized":
        (sub / "feed.csv").write_bytes(b"x" * 1_001)
    elif case == "folder":
        (sub / "feed.csv").mkdir()
    else:
        (sub / "feed.csv").write_bytes(b"x")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "feed.csv").write_bytes(b"secret")
        _swap_before_open(
            monkeypatch,
            0,
            lambda: (sub.rename(upload_dir / "saved"), sub.symlink_to(outside, target_is_directory=True)),
        )
    opened = _track_descriptors(monkeypatch)
    with pytest.raises(ValidationError):
        read("sub/feed.csv")
    _assert_all_closed(opened)


def test_a_folder_on_the_path_needs_only_search_permission(tmp_path, monkeypatch) -> None:
    # An ordinary open by path needs only search (x) permission on parent folders.
    locked = tmp_path / "locked"
    folder = locked / "uploads"
    folder.mkdir(parents=True)
    (folder / "feed.csv").write_bytes(b"x")
    monkeypatch.setenv("META_UPLOAD_DIR", str(folder))
    reload_settings()
    locked.chmod(0o311)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with permissions that ignore folder modes")
        assert read("feed.csv").content == b"x"
    finally:
        locked.chmod(0o755)
