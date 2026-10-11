#!/usr/bin/env python3
"""Resolve small overlaps between similarly oriented rectangular buildings."""

from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET


def unit(vector):
    length = math.hypot(*vector)
    return vector[0] / length, vector[1] / length


def dot(point, axis):
    return point[0] * axis[0] + point[1] * axis[1]


def project(points, axis):
    values = [dot(point, axis) for point in points]
    return min(values), max(values)


def interval_overlap(a, b):
    return min(a[1], b[1]) - max(a[0], b[0])


def rectangle_axes(points):
    return unit((points[1][0] - points[0][0], points[1][1] - points[0][1])), unit(
        (points[2][0] - points[1][0], points[2][1] - points[1][1])
    )


def center(points):
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def bbox(points):
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def bbox_intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def aligned(axes_a, axes_b, tolerance_degrees):
    threshold = math.cos(math.radians(tolerance_degrees))
    return max(abs(axes_a[0][0] * axis[0] + axes_a[0][1] * axis[1]) for axis in axes_b) >= threshold


def sat_penetration(points_a, points_b):
    candidates = list(rectangle_axes(points_a)) + list(rectangle_axes(points_b))
    best = None
    for axis in candidates:
        overlap = interval_overlap(project(points_a, axis), project(points_b, axis))
        if overlap <= 0:
            return None
        if best is None or overlap < best[0]:
            best = overlap, axis
    return best


def rebuild_rectangle(points, target_axis, side_toward_other, inset):
    axes = rectangle_axes(points)
    axis_index = max(range(2), key=lambda i: abs(axes[i][0] * target_axis[0] + axes[i][1] * target_axis[1]))
    axis = axes[axis_index]
    other_axis = axes[1 - axis_index]
    low, high = project(points, axis)
    other_low, other_high = project(points, other_axis)
    if side_toward_other > 0:
        high -= inset
    else:
        low += inset
    if high - low < 2.0:
        return None
    corners = []
    for u, v in ((low, other_low), (high, other_low), (high, other_high), (low, other_high)):
        corners.append((u * axis[0] + v * other_axis[0], u * axis[1] + v * other_axis[1]))
    return corners


def to_local(feature, reference_lat):
    sx = 111_320.0 * math.cos(math.radians(reference_lat))
    sy = 110_540.0
    ring = feature["geometry"]["coordinates"][0]
    return [(lon * sx, lat * sy) for lon, lat in ring[:-1]], sx, sy


def to_geojson_ring(points, sx, sy):
    ring = [(x / sx, y / sy) for x, y in points]
    return ring + [ring[0]]


def count_overlaps(records):
    count = 0
    light = 0
    for i, a in enumerate(records):
        if len(a["points"]) != 4:
            continue
        for b in records[i + 1 :]:
            if len(b["points"]) != 4 or not bbox_intersects(bbox(a["points"]), bbox(b["points"])):
                continue
            penetration = sat_penetration(a["points"], b["points"])
            if penetration:
                count += 1
                light += penetration[0] <= 1.5
    return count, light


def write_osm(base_osm, features, output_osm):
    tree = ET.parse(base_osm)
    root = tree.getroot()
    existing_ids = [int(e.get("id")) for kind in ("node", "way", "relation") for e in root.findall(kind)]
    next_id = min([-1] + [value - 1 for value in existing_ids if value < 0])
    for feature in features:
        ring = feature["geometry"]["coordinates"][0][:-1]
        refs = []
        for lon, lat in ring:
            refs.append(next_id)
            ET.SubElement(root, "node", id=str(next_id), lat=f"{lat:.7f}", lon=f"{lon:.7f}")
            next_id -= 1
        way = ET.SubElement(root, "way", id=str(next_id))
        next_id -= 1
        for ref in refs + [refs[0]]:
            ET.SubElement(way, "nd", ref=str(ref))
        ET.SubElement(way, "tag", k="building", v="yes")
        ET.SubElement(way, "tag", k="source", v="Esri World Imagery; AI-assisted trace")
        ET.SubElement(way, "tag", k="fixme", v="AI-traced candidate; verify in JOSM")
    ET.indent(root, space="  ")
    tree.write(output_osm, encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_osm", type=Path)
    parser.add_argument("input_geojson", type=Path)
    parser.add_argument("output_osm", type=Path)
    parser.add_argument("output_geojson", type=Path)
    parser.add_argument("--max-overlap", type=float, default=1.5)
    parser.add_argument("--gap", type=float, default=0.12)
    parser.add_argument("--orientation-tolerance", type=float, default=6.0)
    args = parser.parse_args()

    base_root = ET.parse(args.base_osm).getroot()
    bounds = base_root.find("bounds")
    reference_lat = (float(bounds.get("minlat")) + float(bounds.get("maxlat"))) / 2
    collection = json.loads(args.input_geojson.read_text(encoding="utf-8"))
    features = deepcopy(collection["features"])
    records = []
    for feature in features:
        points, sx, sy = to_local(feature, reference_lat)
        records.append({"feature": feature, "points": points, "sx": sx, "sy": sy, "adjustments": 0})

    before_total, before_light = count_overlaps(records)
    adjusted_pairs = 0
    # Two passes handle small chains of neighboring buildings without allowing
    # repeated large erosion of any one footprint.
    for _ in range(2):
        changed = False
        for i, a in enumerate(records):
            if len(a["points"]) != 4 or a["adjustments"] >= 2:
                continue
            for b in records[i + 1 :]:
                if len(b["points"]) != 4 or b["adjustments"] >= 2:
                    continue
                if not bbox_intersects(bbox(a["points"]), bbox(b["points"])):
                    continue
                axes_a, axes_b = rectangle_axes(a["points"]), rectangle_axes(b["points"])
                if not aligned(axes_a, axes_b, args.orientation_tolerance):
                    continue
                penetration = sat_penetration(a["points"], b["points"])
                if not penetration or penetration[0] > args.max_overlap:
                    continue
                depth, axis = penetration
                center_a, center_b = center(a["points"]), center(b["points"])
                direction_a = 1 if dot(center_b, axis) > dot(center_a, axis) else -1
                direction_b = 1 if dot(center_a, axis) > dot(center_b, axis) else -1
                inset = (depth + args.gap) / 2
                new_a = rebuild_rectangle(a["points"], axis, direction_a, inset)
                new_b = rebuild_rectangle(b["points"], axis, direction_b, inset)
                if new_a is None or new_b is None:
                    continue
                a["points"], b["points"] = new_a, new_b
                a["adjustments"] += 1
                b["adjustments"] += 1
                adjusted_pairs += 1
                changed = True
        if not changed:
            break

    for record in records:
        record["feature"]["geometry"]["coordinates"][0] = to_geojson_ring(record["points"], record["sx"], record["sy"])
        if record["adjustments"]:
            record["feature"]["properties"]["overlap_adjustments"] = record["adjustments"]

    after_total, after_light = count_overlaps(records)
    result = {
        "type": "FeatureCollection",
        "properties": {
            **collection.get("properties", {}),
            "overlaps_before": before_total,
            "light_overlaps_before": before_light,
            "adjusted_pairs": adjusted_pairs,
            "overlaps_after": after_total,
            "light_overlaps_after": after_light,
            "max_automatic_overlap_m": args.max_overlap,
            "target_gap_m": args.gap,
        },
        "features": [record["feature"] for record in records],
    }
    args.output_geojson.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_osm(args.base_osm, result["features"], args.output_osm)
    print(json.dumps(result["properties"]))


if __name__ == "__main__":
    main()
