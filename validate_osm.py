#!/usr/bin/env python3
"""Validate structural properties of an OSM candidate file."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("osm", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    root = ET.parse(args.osm).getroot()
    nodes = root.findall("node")
    ways = root.findall("way")
    relations = root.findall("relation")
    node_ids = {node.get("id") for node in nodes}
    all_ids = [element.get("id") for element in nodes + ways + relations]
    missing_refs = sorted({nd.get("ref") for way in ways for nd in way.findall("nd")} - node_ids)
    buildings = [way for way in ways if any(tag.get("k") == "building" for tag in way.findall("tag"))]
    open_buildings = [
        way.get("id")
        for way in buildings
        if len(way.findall("nd")) < 4 or way.findall("nd")[0].get("ref") != way.findall("nd")[-1].get("ref")
    ]
    vertices = Counter(max(0, len(way.findall("nd")) - 1) for way in buildings)
    report = {
        "file": str(args.osm.resolve()),
        "nodes": len(nodes),
        "ways": len(ways),
        "relations": len(relations),
        "buildings": len(buildings),
        "building_vertex_distribution": dict(sorted(vertices.items())),
        "duplicate_ids": len(all_ids) - len(set(all_ids)),
        "missing_node_refs": missing_refs,
        "open_buildings": open_buildings,
        "valid": len(all_ids) == len(set(all_ids)) and not missing_refs and not open_buildings,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json:
        args.json.write_text(rendered + "\n", encoding="utf-8")
    if not report["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
