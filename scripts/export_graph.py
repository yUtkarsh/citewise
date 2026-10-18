"""Export a code graph as a FalkorDB dump (Redis DUMP of the graph key, gzipped) with a
manifest of what it holds, for loading elsewhere with ``scripts.load_graph``. Read-only.
The graph's crop images (``data/crops/<graph>/``: equation, table and clause crops the
source panel shows) are files, not graph data; they go in ``<graph>_crops.zip`` beside it.

    python -m scripts.export_graph --code fema356 [--out export]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import time
import zipfile

from app.config import ROOT
from app.graph.connection import get_db

CROPS_ROOT = ROOT / "data" / "crops"
ZIP_DATE = (2026, 1, 1, 0, 0, 0)  # fixed, so the same crops always give the same zip


def pack_crops(src: Path, zip_path: Path) -> dict | None:
    """Zip every file under ``src`` (a ``data/crops/<graph>`` folder) with paths relative to
    its parent, in sorted order with fixed dates. Returns the manifest entry, or None when
    there are no crops."""
    files = sorted(f for f in src.rglob("*") if f.is_file()) if src.is_dir() else []
    if not files:
        return None
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(f.relative_to(src.parent).as_posix(), ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    data = zip_path.read_bytes()
    return {"zip": zip_path.name, "files": len(files), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def counts(graph) -> dict:
    labels = graph.ro_query("MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) ORDER BY l").result_set
    edges = graph.ro_query("MATCH ()-[r]->() RETURN type(r), count(*) ORDER BY type(r)").result_set
    return {"nodes": graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
            "edges": sum(c for _, c in edges), "labels": dict(labels), "edge_types": dict(edges)}


def module_version(db) -> int | None:
    for module in db.connection.execute_command("MODULE", "LIST"):
        fields = dict(zip(module[::2], module[1::2]))
        name = fields.get(b"name", fields.get("name"))
        if name in (b"graph", "graph"):
            return int(fields.get(b"ver", fields.get("ver")))
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--code", default="fema356")
    ap.add_argument("--out", default="export")
    args = ap.parse_args()
    db, key = get_db(), f"code_{args.code}"
    if key not in db.list_graphs():
        raise SystemExit(f"no graph {key} in FalkorDB")
    blob = db.connection.dump(key)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dump = out / f"{key}.dump.gz"
    dump.write_bytes(gzip.compress(blob, 9, mtime=0))
    manifest = {"graph": key, "dump": dump.name, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
                "falkordb_graph_module": module_version(db), "exported": time.strftime("%Y-%m-%d"),
                **counts(db.select_graph(key))}
    if crops := pack_crops(CROPS_ROOT / key, out / f"{key}_crops.zip"):
        manifest["crops"] = crops
    (out / f"{key}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{dump}: {dump.stat().st_size / 1e6:.1f} MB gzipped, {manifest['nodes']} nodes, {manifest['edges']} edges")


if __name__ == "__main__":
    main()
