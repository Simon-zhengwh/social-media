from __future__ import annotations

import builtins
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services import material_filter_files as mf


def _jpg(color=(180, 90, 50), size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _manifest(root: Path) -> list[tuple[str, int, int, str]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            rows.append(
                (
                    str(path.relative_to(root)),
                    stat.st_size,
                    stat.st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return rows


def _within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
        return True
    except ValueError:
        return False


def _guard_source_writes(monkeypatch, source: Path) -> None:
    real_open = builtins.open
    real_os_open = os.open

    def forbidden(*args, **kwargs):
        raise AssertionError(f"source write attempted: {args!r}")

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, Path)) and _within(Path(file), source):
            if any(flag in str(mode) for flag in "wax+"):
                forbidden(file, mode)
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(file, flags, *args, **kwargs):
        if isinstance(file, (str, Path)) and _within(Path(file), source):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                forbidden(file, flags)
        return real_os_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in ("remove", "unlink", "rmdir", "mkdir", "utime", "truncate"):
        original = getattr(os, name)

        def wrapper(path, *args, _original=original, **kwargs):
            if _within(Path(path), source):
                forbidden(path)
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, wrapper)
    for name in ("rename", "replace", "link", "symlink"):
        original = getattr(os, name)

        def wrapper(src, dst, *args, _original=original, **kwargs):
            if _within(Path(src), source) or _within(Path(dst), source):
                forbidden(src, dst)
            return _original(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, name, wrapper)


def test_source_reads_previews_and_local_copies_never_touch_the_source(tmp_path, monkeypatch):
    source = tmp_path / "smb" / "照片"
    (source / "子目录").mkdir(parents=True)
    (source / "a.jpg").write_bytes(_jpg())
    (source / "b.png").write_bytes(_jpg((10, 120, 200)))
    (source / "子目录" / "c.jpg").write_bytes(_jpg((30, 30, 30)))
    (source / "notes.txt").write_text("not an image", encoding="utf-8")
    (source / "noext").write_bytes(_jpg())
    before = _manifest(source)
    output = tmp_path / "local-output"

    _guard_source_writes(monkeypatch, source)

    entries = mf.scan_source(source, recursive=True)
    assert [entry.name for entry in entries] == ["a.jpg", "b.png", "c.jpg"]

    snapshots = [mf.read_source_snapshot(source, entry) for entry in entries]
    assert all(snapshot.data for snapshot in snapshots)
    assert snapshots[0].sha256 == hashlib.sha256((source / "a.jpg").read_bytes()).hexdigest()

    preview = mf.write_local_preview(output / "scheme", "meval_1", snapshots[0].data)
    assert (output / "scheme" / preview).is_file()
    assert (output / "scheme" / preview).read_bytes()[:2] == b"\xff\xd8"

    name, digest = mf.copy_approved_bytes(
        output / "scheme",
        display_name="红烧肉特写",
        short_id="abcd1234",
        suffix=".jpg",
        data=snapshots[0].data,
        sha256=snapshots[0].sha256,
    )
    assert name == "红烧肉特写__abcd1234.jpg"
    assert digest == snapshots[0].sha256
    assert (output / "scheme" / "passed" / name).read_bytes() == snapshots[0].data

    # Same name and bytes reuses the copy; different bytes are refused.
    again, _ = mf.copy_approved_bytes(
        output / "scheme",
        display_name="红烧肉特写",
        short_id="abcd1234",
        suffix=".jpg",
        data=snapshots[0].data,
        sha256=snapshots[0].sha256,
    )
    assert again == name
    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.copy_approved_bytes(
            output / "scheme",
            display_name="红烧肉特写",
            short_id="abcd1234",
            suffix=".jpg",
            data=snapshots[1].data,
            sha256=snapshots[1].sha256,
        )
    assert excinfo.value.code == "output_conflict"

    assert _manifest(source) == before


def test_scan_respects_recursive_switch(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "top.jpg").write_bytes(_jpg())
    (source / "nested" / "deep.jpg").write_bytes(_jpg())
    flat = mf.scan_source(source, recursive=False)
    assert [entry.name for entry in flat] == ["top.jpg"]
    deep = mf.scan_source(source, recursive=True)
    assert sorted(entry.name for entry in deep) == ["deep.jpg", "top.jpg"]


def test_read_source_snapshot_detects_change_during_read(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "changed.jpg"
    path.write_bytes(_jpg())
    entry = mf.scan_source(source)[0]
    real_stat = Path.stat
    calls = {"count": 0}

    def flaky_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == path:
            calls["count"] += 1
            if calls["count"] == 2:
                return SimpleNamespace(
                    st_size=result.st_size, st_mtime_ns=result.st_mtime_ns + 1
                )
        return result

    monkeypatch.setattr(Path, "stat", flaky_stat)
    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.read_source_snapshot(source, entry)
    assert excinfo.value.code == "source_changed"


def test_prepare_model_image_downscales_and_keeps_exif_orientation():
    buffer = io.BytesIO()
    image = Image.new("RGB", (4000, 3000), (200, 120, 60))
    exif = image.getexif()
    exif[274] = 6  # rotate 90 degrees on display
    image.save(buffer, format="JPEG", exif=exif)
    data_url = mf.prepare_model_image_data_url(buffer.getvalue())
    assert data_url.startswith("data:image/jpeg;base64,")
    assert "\\\\" not in data_url
    import base64

    encoded = base64.b64decode(data_url.split(",", 1)[1])
    with Image.open(io.BytesIO(encoded)) as prepared:
        assert prepared.height > prepared.width
        assert max(prepared.size) <= 1536


def test_corrupt_image_fails_without_writing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "broken.jpg").write_bytes(b"not really a jpeg")
    entry = mf.scan_source(source)[0]
    snapshot = mf.read_source_snapshot(source, entry)
    output = tmp_path / "output"
    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.build_preview_jpeg(snapshot.data)
    assert excinfo.value.code == "source_invalid_image"
    assert not output.exists()


def test_normalize_smb_source_accepts_equivalent_chinese_paths():
    unc = "\\\\192.168.11.5\\movies\\See Joy素材\\照片"
    file_url = "file://192.168.11.5/movies/See%20Joy%E7%B4%A0%E6%9D%90/%E7%85%A7%E7%89%87"
    smb_url = "smb://192.168.11.5/movies/See Joy素材/照片/"
    forms = [mf.normalize_smb_source(value) for value in (unc, file_url, smb_url)]
    assert {item.unc for item in forms} == {unc}
    assert len({item.key for item in forms}) == 1
    assert forms[0].display_name == "照片"
    assert forms[0].server == "192.168.11.5"
    assert forms[0].share == "movies"

    share_only = mf.normalize_smb_source("\\\\nas\\照片")
    assert share_only.unc == "\\\\nas\\照片"
    assert share_only.display_name == "照片"
    assert share_only.relative == ""


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "source_required"),
        ("\\\\192.168.11.5", "source_share_required"),
        ("smb://192.168.11.5/", "source_share_required"),
        ("http://192.168.11.5/movies", "source_unsupported_scheme"),
        ("ftp://192.168.11.5/movies", "source_unsupported_scheme"),
        ("smb://user:secret@192.168.11.5/movies", "source_credentials_rejected"),
        ("\\\\user:secret@192.168.11.5\\movies", "source_credentials_rejected"),
        ("\\\\192.168.11.5\\movies\\..\\other", "source_traversal"),
        ("smb://192.168.11.5/movies/../other", "source_traversal"),
        ("\\\\?\\UNC\\192.168.11.5\\movies", "source_device_path"),
        ("C:\\photos", "source_not_shared"),
        ("file:///D:/photos", "source_server_required"),
        ("smb://192.168.11.5:445/movies", "source_invalid"),
        ("smb://192.168.11.5/movies?user=1", "source_invalid"),
        ("smb://192.168.11.5/movies/bad|name", "source_invalid"),
    ],
)
def test_normalize_smb_source_rejects_unsafe_forms(value, code):
    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.normalize_smb_source(value)
    assert excinfo.value.code == code


def test_validate_local_output_root_rejects_network_device_and_traversal(tmp_path):
    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.validate_local_output_root("\\\\192.168.11.5\\movies\\out")
    assert excinfo.value.code == "output_network_path_rejected"

    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.validate_local_output_root("\\\\?\\D:\\out")
    assert excinfo.value.code == "output_device_path"

    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.validate_local_output_root("relative/out")
    assert excinfo.value.code == "output_not_absolute"

    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.validate_local_output_root("D:\\local", drive_type=lambda _: 4)
    assert excinfo.value.code == "output_network_drive_rejected"

    with pytest.raises(mf.MaterialFilterFileError) as excinfo:
        mf.validate_local_output_root(tmp_path, final_path=lambda _: Path("\\\\nas\\share"))
    assert excinfo.value.code == "output_network_path_rejected"

    resolved = mf.validate_local_output_root(
        tmp_path, drive_type=lambda _: 3, final_path=lambda path: path
    )
    assert resolved == tmp_path

    created = mf.ensure_local_directory(
        tmp_path / "made", drive_type=lambda _: 3, final_path=lambda path: path
    )
    assert created.is_dir()


def test_scan_does_not_write_when_source_is_unavailable(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    output = tmp_path / "output"
    with pytest.raises(mf.MaterialFilterFileError):
        mf.scan_source(missing)
    with pytest.raises(mf.MaterialFilterFileError):
        mf.check_source_readable(missing)
    assert not output.exists()
