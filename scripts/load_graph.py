"""Load an exported code graph (``scripts.export_graph``) into FalkorDB and check it
against its manifest: checksum before loading, node and edge counts after. Then unpack
the graph's crop images (``<graph>_crops.zip``, checksum checked) into ``data/crops/``,
keeping files that are already there.

    python -m scripts.load_graph [export/code_fema356.manifest.json] [--replace]

Refuses to overwrite an existing graph of the same name unless ``--replace`` is given.
Needs a FalkorDB whose graph module is the export's version or newer (docker compose up -d).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import zipfile

from app.graph.connection import get_db
from scripts.export_graph import CROPS_ROOT, counts, module_version


def unpack_crops(zip_path: Path, sha256: str, dest: Path) -> tuple[int, int]:
    """Extract the crop zip into ``dest`` after checking its checksum; every path must stay
    inside ``dest``. Existing files are kept. Returns (written, kept)."""
    if hashlib.sha256(zip_path.read_bytes()).hexdigest() != sha256:
        raise SystemExit(f"{zip_path.name}: checksum does not match the manifest")
    root, written, kept = dest.resolve(), 0, 0
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise SystemExit(f"{zip_path.name}: {name!r} would land outside {dest}")
            if name.endswith("/"):
                continue
            if target.exists():
                kept += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
            written += 1
    return written, kept


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", nargs="?", default="export/code_fema356.manifest.json")
    ap.add_argument("--replace", action="store_true", help="overwrite a graph of the same name")
    args = ap.parse_args()
    path = Path(args.manifest)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    blob = gzip.decompress((path.parent / manifest["dump"]).read_bytes())
    if hashlib.sha256(blob).hexdigest() != manifest["sha256"]:
        raise SystemExit(f"{manifest['dump']}: checksum does not match the manifest")
    db, key = get_db(), manifest["graph"]
    have, need = module_version(db), manifest.get("falkordb_graph_module")
    if have is not None and need is not None and have < need:
        raise SystemExit(f"this FalkorDB's graph module is {have}; the dump needs {need} or newer "
                         "(docker compose pull; docker compose up -d)")
    if db.connection.exists(key) and not args.replace:
        raise SystemExit(f"{key} already exists; pass --replace to overwrite it")
    if args.replace:
        db.connection.restore(key, 0, blob, replace=True)
    else:
        db.connection.restore(key, 0, blob)
    got = counts(db.select_graph(key))
    if (got["nodes"], got["edges"]) != (manifest["nodes"], manifest["edges"]):
        raise SystemExit(f"loaded {key} but counts differ: {got['nodes']} nodes, {got['edges']} edges "
                         f"(expected {manifest['nodes']}, {manifest['edges']})")
    print(f"loaded {key}: {got['nodes']} nodes, {got['edges']} edges, matching the manifest")
    if crops := manifest.get("crops"):
        written, kept = unpack_crops(path.parent / crops["zip"], crops["sha256"], CROPS_ROOT)
        print(f"crops: {written} written to {CROPS_ROOT}, {kept} already there")


if __name__ == "__main__":
    main()
