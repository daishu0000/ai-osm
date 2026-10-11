#!/usr/bin/env python3
"""Quality-filter, deduplicate, and append overlapping building-tile results."""

from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET


def local_ring(feature: dict, reference_lat: float):
    scale_x = 111_320.0 * math.cos(math.radians(reference_lat))
    scale_y = 110_540.0
    return [(lon * scale_x, lat * scale_y) for lon, lat in feature["geometry"]["coordinates"][0]]


def area(ring):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))) / 2


def signed_area(points):
    closed = points + [points[0]]
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(closed, closed[1:])) / 2


def line_intersection(start, end, clip_start, clip_end):
    dx1, dy1 = end[0] - start[0], end[1] - start[1]
    dx2, dy2 = clip_end[0] - clip_start[0], clip_end[1] - clip_start[1]
    denominator = dx1 * dy2 - dy1 * dx2
    if abs(denominator) < 1e-10:
        return end
    t = ((clip_start[0] - start[0]) * dy2 - (clip_start[1] - start[1]) * dx2) / denominator
    return start[0] + t * dx1, start[1] + t * dy1


def convex_intersection(subject, clip):
    """Sutherland-Hodgman intersection; clip must be a convex polygon."""
    output = list(subject)
    sign = 1 if signed_area(clip) >= 0 else -1
    for clip_start, clip_end in zip(clip, clip[1:] + clip[:1]):
        input_points = output
        output = []
        if not input_points:
            break

        def inside(point):
            cross = (clip_end[0] - clip_start[0]) * (point[1] - clip_start[1]) - (clip_end[1] - clip_start[1]) * (point[0] - clip_start[0])
            return sign * cross >= -1e-9

        previous = input_points[-1]
        for current in input_points:
            current_inside = inside(current)
            previous_inside = inside(previous)
            if current_inside:
                if not previous_inside:
                    output.append(line_intersection(previous, current, clip_start, clip_end))
                output.append(current)
            elif previous_inside:
                output.append(line_intersection(previous, current, clip_start, clip_end))
            previous = current
    return output


def centroid(ring):
    points = ring[:-1]
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def bbox(ring):
    return min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)


def bbox_iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = ix * iy
    if intersection == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return intersection / (area_a + area_b - intersection)


def bbox_intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def orientation(a, b, c):
    value = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
    if abs(value) < 1e-8:
        return 0
    return 1 if value > 0 else 2


def segments_intersect(a, b, c, d):
    return orientation(a, b, c) != orientation(a, b, d) and orientation(c, d, a) != orientation(c, d, b)


def self_intersects(ring):
    edges = list(zip(ring, ring[1:]))
    for i, (a, b) in enumerate(edges):
        for j, (c, d) in enumerate(edges):
            if abs(i - j) <= 1 or {i, j} == {0, len(edges) - 1}:
                continue
            if segments_intersect(a, b, c, d):
                return True
    return False


def orthogonal_turns(ring, tolerance_degrees=22.0):
    points = ring[:-1]
    for index, point in enumerate(points):
        previous = points[index - 1]
        following = points[(index + 1) % len(points)]
        incoming = (previous[0] - point[0], previous[1] - point[1])
        outgoing = (following[0] - point[0], following[1] - point[1])
        denominator = math.hypot(*incoming) * math.hypot(*outgoing)
        if denominator == 0:
            return False
        angle = math.degrees(math.acos(max(-1.0, min(1.0, (incoming[0] * outgoing[0] + incoming[1] * outgoing[1]) / denominator))))
        if abs(angle - 90.0) > tolerance_degrees:
            return False
    return True


def quality_record(feature, reference_lat):
    ring = local_ring(feature, reference_lat)
    vertices = len(ring) - 1
    polygon_area = area(ring)
    # Outlined building complexes commonly contain notches, wings, and diagonal
    # edges.  Treat those as valid geometry instead of requiring every candidate
    # to look like a small orthogonal rectangle.
    if not 3 <= vertices <= 64:
        return None
    if not 8.0 <= polygon_area <= 8_000.0:
        return None
    if self_intersects(ring):
        return None
    bounds = bbox(ring)
    center = centroid(ring)
    diagonal = math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1])
    # Prefer concise shapes, then larger/clearer candidates in overlap conflicts.
    score = (100 if vertices == 4 else 0) + (10 if orthogonal_turns(ring) else 0) - vertices + min(polygon_area, 500) / 500
    return {
        "feature": feature,
        "ring": ring,
        "points": ring[:-1],
        "bbox": bounds,
        "centroid": center,
        "diagonal": diagonal,
        "area": polygon_area,
        "score": score,
    }


def duplicates(a, b):
    if not bbox_intersects(a["bbox"], b["bbox"]):
        return False
    source_a = a["feature"]["properties"].get("candidate_source")
    source_b = b["feature"]["properties"].get("candidate_source")
    if len(a["points"]) == 4 and len(b["points"]) == 4:
        intersection_polygon = convex_intersection(a["points"], b["points"])
        intersection_area = abs(signed_area(intersection_polygon)) if len(intersection_polygon) >= 3 else 0.0
        if intersection_area:
            smaller_coverage = intersection_area / min(a["area"], b["area"])
            union = a["area"] + b["area"] - intersection_area
            polygon_iou = intersection_area / union if union else 0.0
            # Cross-tile detections of one roof often differ in size or center.
            # Large coverage is a duplicate even when the centroids do not match.
            if source_a != source_b and (smaller_coverage >= 0.55 or polygon_iou >= 0.35):
                return True
            # Near-containment is never useful as two separate building outlines.
            if smaller_coverage >= 0.88:
                return True
    distance = math.dist(a["centroid"], b["centroid"])
    size_limit = max(2.0, 0.28 * min(a["diagonal"], b["diagonal"]))
    return distance <= size_limit and bbox_iou(a["bbox"], b["bbox"]) >= 0.28


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_osm", type=Path)
    parser.add_argument("output_osm", type=Path)
    parser.add_argument("output_geojson", type=Path)
    parser.add_argument("candidate_geojson", nargs="+", type=Path)
    args = parser.parse_args()

    tree = ET.parse(args.base_osm)
    root = tree.getroot()
    bounds_element = root.find("bounds")
    if bounds_element is None:
        raise SystemExit("The base OSM file has no bounds")
    min_lat = float(bounds_element.get("minlat"))
    min_lon = float(bounds_element.get("minlon"))
    max_lat = float(bounds_element.get("maxlat"))
    max_lon = float(bounds_element.get("maxlon"))
    reference_lat = (min_lat + max_lat) / 2

    raw_features = []
    for path in args.candidate_geojson:
        data = json.loads(path.read_text(encoding="utf-8"))
        for feature in data["features"]:
            copied = deepcopy(feature)
            copied["properties"]["candidate_source"] = path.stem
            raw_features.append(copied)

    inside_bounds = []
    outside = 0
    quality_rejected = 0
    for feature in raw_features:
        ring = feature["geometry"]["coordinates"][0]
        if not all(min_lon <= lon <= max_lon and min_lat <= lat <= max_lat for lon, lat in ring):
            outside += 1
            continue
        record = quality_record(feature, reference_lat)
        if record is None:
            quality_rejected += 1
            continue
        inside_bounds.append(record)

    selected = []
    duplicates_removed = 0
    for candidate in sorted(inside_bounds, key=lambda item: item["score"], reverse=True):
        if any(duplicates(candidate, existing) for existing in selected):
            duplicates_removed += 1
        else:
            selected.append(candidate)

    existing_ids = [int(e.get("id")) for kind in ("node", "way", "relation") for e in root.findall(kind)]
    next_id = min([-1] + [value - 1 for value in existing_ids if value < 0])
    for record in selected:
        feature = record["feature"]
        ring = feature["geometry"]["coordinates"][0]
        coordinates = ring[:-1]
        refs = []
        for lon, lat in coordinates:
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

    features = [record["feature"] for record in selected]
    result_geojson = {
        "type": "FeatureCollection",
        "properties": {
            "raw_candidates": len(raw_features),
            "outside_bounds": outside,
            "quality_rejected": quality_rejected,
            "duplicates_removed": duplicates_removed,
            "accepted": len(features),
        },
        "features": features,
    }
    args.output_geojson.write_text(json.dumps(result_geojson, ensure_ascii=False, indent=2), encoding="utf-8")
    ET.indent(root, space="  ")
    tree.write(args.output_osm, encoding="utf-8", xml_declaration=True)
    print(json.dumps(result_geojson["properties"]))


if __name__ == "__main__":
    main()
