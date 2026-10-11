#!/usr/bin/env python3
"""Run the complete post-processing pipeline from outlined images to OSM."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def resolved(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert outlined XYZ mosaics into a reviewed OSM candidate file")
    parser.add_argument("manifest", type=Path, help="JSON manifest; relative paths are resolved from its directory")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    manifest_dir = manifest_path.parent
    config = json.loads(manifest_path.read_text(encoding="utf-8"))
    script_dir = Path(__file__).resolve().parent
    base_osm = resolved(manifest_dir, config["base_osm"])
    output_dir = resolved(manifest_dir, config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    if not base_osm.is_file():
        raise SystemExit(f"Base OSM file not found: {base_osm}")
    chunks = config.get("chunks", [])
    if not chunks:
        raise SystemExit("Manifest must contain at least one chunk")

    vector = config.get("vectorize", {})
    chunk_geojsons = []
    for chunk in chunks:
        name = chunk["name"]
        image = resolved(manifest_dir, chunk["image"])
        if not image.is_file():
            raise SystemExit(f"Outlined image not found: {image}")
        geojson = output_dir / f"{name}-buildings.geojson"
        osm = output_dir / f"{name}-buildings.osm"
        command = [
            sys.executable,
            str(script_dir / "vectorize_outlines.py"),
            str(image),
            "--x", str(chunk["x"]),
            "--y", str(chunk["y"]),
            "--z", str(chunk.get("z", 18)),
            "--tiles-wide", str(chunk["tiles_wide"]),
            "--tiles-high", str(chunk["tiles_high"]),
            "--min-area", str(vector.get("min_area", 50)),
            "--simplify", str(vector.get("simplify", 2.0)),
            "--edge-margin", str(vector.get("edge_margin", 8)),
            "--dilate", str(vector.get("dilate", 1)),
            "--color-mode", str(vector.get("color_mode", "strict")),
            "--orientation-radius", str(vector.get("orientation_radius", 120.0)),
            "--orientation-tolerance", str(vector.get("orientation_tolerance", 8.0)),
            "--geojson", str(geojson),
            "--osm", str(osm),
        ]
        run(command)
        chunk_geojsons.append(geojson)

    deduplicated_osm = output_dir / "buildings-deduplicated.osm"
    deduplicated_geojson = output_dir / "buildings-deduplicated.geojson"
    run([
        sys.executable,
        str(script_dir / "merge_building_tiles.py"),
        str(base_osm),
        str(deduplicated_osm),
        str(deduplicated_geojson),
        *map(str, chunk_geojsons),
    ])

    overlap = config.get("overlap_resolution", {})
    final_osm = output_dir / "buildings-final.osm"
    final_geojson = output_dir / "buildings-final.geojson"
    run([
        sys.executable,
        str(script_dir / "resolve_building_overlaps.py"),
        str(base_osm),
        str(deduplicated_geojson),
        str(final_osm),
        str(final_geojson),
        "--max-overlap", str(overlap.get("max_overlap_m", 1.5)),
        "--gap", str(overlap.get("gap_m", 0.12)),
        "--orientation-tolerance", str(overlap.get("orientation_tolerance", 6.0)),
    ])

    report_path = output_dir / "validation.json"
    run([
        sys.executable,
        str(script_dir / "validate_osm.py"),
        str(final_osm),
        "--json", str(report_path),
    ])
    print(f"Final OSM: {final_osm}")
    print(f"Final GeoJSON: {final_geojson}")
    print(f"Validation: {report_path}")


if __name__ == "__main__":
    main()
