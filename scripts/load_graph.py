"""Load an exported code graph (``scripts.export_graph``) into FalkorDB and check it
against its manifest: checksum before loading, node and edge counts after.

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

from app.graph.connection import get_db
from scripts.export_graph import counts, module_version


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


if __name__ == "__main__":
    main()
