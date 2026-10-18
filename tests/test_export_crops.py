"""Crop images travel with an exported graph: packed deterministically, unpacked with a
checksum check, never outside the crops folder, existing files kept."""
from __future__ import annotations

import hashlib
import zipfile

import pytest

from scripts.export_graph import pack_crops
from scripts.load_graph import unpack_crops


def test_crops_round_trip_and_the_zip_is_deterministic(tmp_path):
    src = tmp_path / "crops" / "code_x"
    (src / "equations").mkdir(parents=True)
    (src / "equations" / "Equation_1-1_p3.png").write_bytes(b"png-1")
    (src / "tables" / "t").mkdir(parents=True)
    (src / "tables" / "t" / "Table_1-1_0_p4.png").write_bytes(b"png-2")
    first = pack_crops(src, tmp_path / "a.zip")
    second = pack_crops(src, tmp_path / "b.zip")
    assert first["files"] == 2 and first["sha256"] == second["sha256"]
    dest = tmp_path / "loaded"
    (dest / "code_x" / "equations").mkdir(parents=True)
    (dest / "code_x" / "equations" / "Equation_1-1_p3.png").write_bytes(b"local")
    assert unpack_crops(tmp_path / "a.zip", first["sha256"], dest) == (1, 1)
    assert (dest / "code_x" / "tables" / "t" / "Table_1-1_0_p4.png").read_bytes() == b"png-2"
    assert (dest / "code_x" / "equations" / "Equation_1-1_p3.png").read_bytes() == b"local"  # kept


def test_a_bad_checksum_or_an_escaping_path_is_refused(tmp_path):
    assert pack_crops(tmp_path / "missing", tmp_path / "none.zip") is None
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../outside.png", b"x")
    with pytest.raises(SystemExit, match="checksum"):
        unpack_crops(bad, "0" * 64, tmp_path / "dest")
    with pytest.raises(SystemExit, match="outside"):
        unpack_crops(bad, hashlib.sha256(bad.read_bytes()).hexdigest(), tmp_path / "dest")
    assert not (tmp_path / "outside.png").exists()
