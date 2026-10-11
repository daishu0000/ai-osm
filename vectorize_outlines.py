#!/usr/bin/env python3
"""Vectorize magenta building outlines drawn on an XYZ web-map tile.

The script deliberately uses only Pillow, NumPy, and the Python standard
library. It detects closed magenta outlines, extracts their enclosed regions,
converts their pixel boundaries to lon/lat, and writes GeoJSON and OSM XML.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict, deque
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image


def dilate(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    padded = np.pad(mask, radius, constant_values=False)
    result = np.zeros_like(mask)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            result |= padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return result


def enclosed_components(blocked: np.ndarray, min_area: int) -> list[set[tuple[int, int]]]:
    """Return non-blocked components that do not touch the image border."""
    height, width = blocked.shape
    seen = np.zeros_like(blocked)
    components: list[set[tuple[int, int]]] = []

    for start_y in range(height):
        for start_x in range(width):
            if blocked[start_y, start_x] or seen[start_y, start_x]:
                continue
            queue = deque([(start_x, start_y)])
            seen[start_y, start_x] = True
            pixels: set[tuple[int, int]] = set()
            touches_border = False
            while queue:
                x, y = queue.popleft()
                pixels.add((x, y))
                touches_border |= x == 0 or y == 0 or x == width - 1 or y == height - 1
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < width and 0 <= ny < height:
                        if not blocked[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            queue.append((nx, ny))
            if not touches_border and len(pixels) >= min_area:
                components.append(pixels)
    return components


def component_ring(pixels: set[tuple[int, int]]) -> list[tuple[float, float]]:
    """Trace the longest cell-edge ring surrounding a raster component."""
    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for x, y in pixels:
        if (x, y - 1) not in pixels:
            edges.append(((x, y), (x + 1, y)))
        if (x + 1, y) not in pixels:
            edges.append(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in pixels:
            edges.append(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in pixels:
            edges.append(((x, y + 1), (x, y)))

    outgoing: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for start, end in edges:
        outgoing[start].append(end)

    rings: list[list[tuple[int, int]]] = []
    unused = set(edges)
    while unused:
        start, current = next(iter(unused))
        ring = [start, current]
        unused.remove((start, current))
        while current != start:
            candidates = [end for end in outgoing[current] if (current, end) in unused]
            if not candidates:
                break
            nxt = candidates[0]
            unused.remove((current, nxt))
            ring.append(nxt)
            current = nxt
        if ring[-1] == start:
            rings.append(ring)
    if not rings:
        return []
    return [(float(x), float(y)) for x, y in max(rings, key=len)]


def point_line_distance(point, start, end) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    start, end = points[0], points[-1]
    distances = [point_line_distance(point, start, end) for point in points[1:-1]]
    if not distances or max(distances) <= epsilon:
        return [start, end]
    index = distances.index(max(distances)) + 1
    return rdp(points[: index + 1], epsilon)[:-1] + rdp(points[index:], epsilon)


def simplify_closed_ring(ring: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(ring) < 4:
        return ring
    open_ring = ring[:-1]
    anchor = min(range(len(open_ring)), key=lambda i: (open_ring[i][0], open_ring[i][1]))
    rotated = open_ring[anchor:] + open_ring[:anchor] + [open_ring[anchor]]
    simplified = rdp(rotated, epsilon)
    if simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified


def angle_distance(a: float, b: float) -> float:
    """Smallest angular distance for orientations periodic every 90 degrees."""
    return abs(((a - b + math.pi / 4) % (math.pi / 2)) - math.pi / 4)


def ring_orientation(ring: list[tuple[float, float]]) -> float:
    """Estimate a building's dominant axis, normalized to [0, 90 degrees)."""
    sx = sy = 0.0
    for start, end in zip(ring, ring[1:]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 2.0:
            continue
        angle = math.atan2(dy, dx) % (math.pi / 2)
        weight = length * length
        sx += weight * math.cos(4 * angle)
        sy += weight * math.sin(4 * angle)
    return (math.atan2(sy, sx) / 4) % (math.pi / 2)


def shared_orientations(
    rings: list[list[tuple[float, float]]],
    tolerance_degrees: float = 8.0,
    spatial_radius: float = 120.0,
):
    """Estimate a local shared axis using only nearby, similarly oriented roofs.

    Each building gets its own neighborhood rather than joining a global angle
    cluster. This prevents two distant parts of a large image from being forced
    to the same direction merely because their angles happen to be similar.
    """
    tolerance = math.radians(tolerance_degrees)
    orientations = [ring_orientation(ring) for ring in rings]
    centers = []
    for ring in rings:
        points = ring[:-1]
        centers.append((sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)))

    result = list(orientations)
    neighborhood_sizes = []
    adjusted = 0
    for index, (orientation, center) in enumerate(zip(orientations, centers)):
        neighbors = [
            other
            for other, (other_orientation, other_center) in enumerate(zip(orientations, centers))
            if math.dist(center, other_center) <= spatial_radius
            and angle_distance(orientation, other_orientation) <= tolerance
        ]
        neighborhood_sizes.append(len(neighbors))
        if len(neighbors) >= 3:
            local_axis = circular_orientation([orientations[other] for other in neighbors])
            # Limit the adjustment even inside a valid local group.
            if angle_distance(orientation, local_axis) <= tolerance:
                result[index] = local_axis
                adjusted += 1

    summary = {
        "mode": "local-neighborhood",
        "spatial_radius_pixels": spatial_radius,
        "angle_tolerance_degrees": tolerance_degrees,
        "minimum_neighbors": 3,
        "locally_adjusted": adjusted,
        "isolated_or_unmatched": len(rings) - adjusted,
        "max_neighborhood": max(neighborhood_sizes, default=0),
    }
    return result, [summary]


def circular_orientation(angles: list[float]) -> float:
    sx = sum(math.cos(4 * angle) for angle in angles)
    sy = sum(math.sin(4 * angle) for angle in angles)
    return (math.atan2(sy, sx) / 4) % (math.pi / 2)


def polygon_area(ring: list[tuple[float, float]]) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))) / 2


def clean_collinear(ring: list[tuple[float, float]], tolerance_degrees: float = 6.0):
    points = ring[:-1]
    changed = True
    while changed and len(points) > 3:
        changed = False
        kept = []
        for i, point in enumerate(points):
            previous = points[i - 1]
            following = points[(i + 1) % len(points)]
            a = math.atan2(point[1] - previous[1], point[0] - previous[0])
            b = math.atan2(following[1] - point[1], following[0] - point[0])
            turn = abs(((b - a + math.pi) % (2 * math.pi)) - math.pi)
            if min(turn, abs(math.pi - turn)) < math.radians(tolerance_degrees):
                changed = True
            else:
                kept.append(point)
        points = kept
    return points + [points[0]] if points else []


def line_intersection(p1, direction1, p2, direction2):
    cross = direction1[0] * direction2[1] - direction1[1] * direction2[0]
    if abs(cross) < 1e-6:
        return None
    delta = (p2[0] - p1[0], p2[1] - p1[1])
    t = (delta[0] * direction2[1] - delta[1] * direction2[0]) / cross
    return p1[0] + t * direction1[0], p1[1] + t * direction1[1]


def orthogonalize(ring: list[tuple[float, float]], orientation: float):
    """Snap consecutive polygon edges to two perpendicular shared axes."""
    ring = clean_collinear(ring)
    points = ring[:-1]
    if len(points) < 3:
        return ring
    axes = [
        (math.cos(orientation), math.sin(orientation)),
        (math.cos(orientation + math.pi / 2), math.sin(orientation + math.pi / 2)),
    ]

    # Most rural roofs are rectangles. For a footprint that substantially fills
    # its oriented bounding box, use exactly four shared-axis corners. This is
    # much more stable than fitting every anti-aliased raster stair-step.
    projected_u = [point[0] * axes[0][0] + point[1] * axes[0][1] for point in points]
    projected_v = [point[0] * axes[1][0] + point[1] * axes[1][1] for point in points]
    min_u, max_u = min(projected_u), max(projected_u)
    min_v, max_v = min(projected_v), max(projected_v)
    box_area = (max_u - min_u) * (max_v - min_v)
    fill_ratio = polygon_area(ring) / box_area if box_area else 0.0
    if fill_ratio >= 0.68:
        corners = []
        for u, v in ((min_u, min_v), (max_u, min_v), (max_u, max_v), (min_u, max_v)):
            corners.append((u * axes[0][0] + v * axes[1][0], u * axes[0][1] + v * axes[1][1]))
        corners.append(corners[0])
        return corners

    lines = []
    for start, end in zip(points, points[1:] + points[:1]):
        edge = (end[0] - start[0], end[1] - start[1])
        axis = max(axes, key=lambda candidate: abs(edge[0] * candidate[0] + edge[1] * candidate[1]))
        if edge[0] * axis[0] + edge[1] * axis[1] < 0:
            axis = (-axis[0], -axis[1])
        midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        lines.append((midpoint, axis))

    result = []
    for index in range(len(lines)):
        intersection = line_intersection(*lines[index - 1], *lines[index])
        if intersection is None:
            return ring
        result.append(intersection)
    result.append(result[0])

    original_area = polygon_area(ring)
    new_area = polygon_area(result)
    maximum_move = max(math.dist(a, b) for a, b in zip(points, result[:-1]))
    if original_area == 0 or not 0.55 <= new_area / original_area <= 1.6 or maximum_move > 35:
        return ring
    return clean_collinear(result, tolerance_degrees=4.0)


def tile_pixel_to_lonlat(tile_x: int, tile_y: int, zoom: int, px: float, py: float):
    scale = 256 * (2**zoom)
    global_x = tile_x * 256 + px
    global_y = tile_y * 256 + py
    lon = global_x / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * global_y / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def write_osm(features: list[dict], destination: Path, source: str) -> None:
    root = ET.Element("osm", version="0.6", generator="josm-ai-edit prototype")
    next_node_id = -1
    next_way_id = -1
    for feature in features:
        refs = []
        for lon, lat in feature["geometry"]["coordinates"][0][:-1]:
            node = ET.SubElement(root, "node", id=str(next_node_id), lat=f"{lat:.7f}", lon=f"{lon:.7f}")
            refs.append(next_node_id)
            next_node_id -= 1
        if len(refs) < 3:
            continue
        way = ET.SubElement(root, "way", id=str(next_way_id))
        next_way_id -= 1
        for ref in refs + [refs[0]]:
            ET.SubElement(way, "nd", ref=str(ref))
        ET.SubElement(way, "tag", k="building", v="yes")
        ET.SubElement(way, "tag", k="source", v=source)
        ET.SubElement(way, "tag", k="fixme", v="AI-traced candidate; verify in JOSM")
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--z", type=int, required=True)
    parser.add_argument("--tiles-wide", type=int, default=1)
    parser.add_argument("--tiles-high", type=int, default=1)
    parser.add_argument("--geojson", type=Path, required=True)
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--min-area", type=int, default=500)
    parser.add_argument("--simplify", type=float, default=4.0)
    parser.add_argument("--edge-margin", type=int, default=8)
    parser.add_argument("--dilate", type=int, default=1)
    parser.add_argument("--color-mode", choices=("strict", "broad"), default="strict")
    parser.add_argument("--orientation-radius", type=float, default=120.0)
    parser.add_argument("--orientation-tolerance", type=float, default=8.0)
    args = parser.parse_args()

    rgb = np.asarray(Image.open(args.image).convert("RGB"))
    height, width = rgb.shape[:2]
    # Include anti-aliased magenta edge pixels while rejecting red/orange roofs
    # and blue roofs: both red and blue must be substantially above green.
    channels = rgb.astype(np.int16)
    red, green, blue = channels[:, :, 0], channels[:, :, 1], channels[:, :, 2]
    if args.color_mode == "strict":
        magenta = (red >= 190) & (green <= 105) & (blue >= 150) & (red - green >= 90)
    else:
        magenta = (red >= 150) & (blue >= 100) & (red - green >= 50) & (blue - green >= 30)
    blocked = dilate(magenta, radius=args.dilate)
    components = enclosed_components(blocked, args.min_area)

    rings = []
    for component in components:
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        if min(xs) <= args.edge_margin or min(ys) <= args.edge_margin:
            continue
        if max(xs) >= width - 1 - args.edge_margin or max(ys) >= height - 1 - args.edge_margin:
            continue
        ring = simplify_closed_ring(component_ring(component), args.simplify)
        if len(ring) < 4:
            continue
        rings.append(clean_collinear(ring))

    raw_orientations = [ring_orientation(ring) for ring in rings]
    orientations, orientation_groups = shared_orientations(
        rings,
        tolerance_degrees=args.orientation_tolerance,
        spatial_radius=args.orientation_radius,
    )
    rings = [orthogonalize(ring, orientation) for ring, orientation in zip(rings, orientations)]

    features = []
    for index, (ring, orientation, raw_orientation) in enumerate(zip(rings, orientations, raw_orientations), start=1):
        # Map a possibly resized edited mosaic back to its XYZ tile-pixel extent.
        tile_ring = [
            (x * (256.0 * args.tiles_wide) / width, y * (256.0 * args.tiles_high) / height)
            for x, y in ring
        ]
        coordinates = [tile_pixel_to_lonlat(args.x, args.y, args.z, x, y) for x, y in tile_ring]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": index,
                    "building": "yes",
                    "orientation": round(math.degrees(orientation), 2),
                    "raw_orientation": round(math.degrees(raw_orientation), 2),
                    "orientation_adjustment": round(math.degrees(angle_distance(orientation, raw_orientation)), 2),
                    "vertex_count": len(ring) - 1,
                },
                "geometry": {"type": "Polygon", "coordinates": [coordinates]},
            }
        )

    collection = {
        "type": "FeatureCollection",
        "properties": {
            "source_image": str(args.image),
            "xyz": {"x": args.x, "y": args.y, "z": args.z},
            "edited_size": [width, height],
            "tile_grid": [args.tiles_wide, args.tiles_high],
            "detected_outline_pixels": int(magenta.sum()),
            "orientation_groups": orientation_groups,
        },
        "features": features,
    }
    args.geojson.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")
    write_osm(features, args.osm, "Esri World Imagery; AI-assisted trace")
    print(json.dumps({
        "image_size": [width, height],
        "magenta_pixels": int(magenta.sum()),
        "features": len(features),
        "vertices": sum(feature["properties"]["vertex_count"] for feature in features),
        "orientation_groups": orientation_groups,
    }))


if __name__ == "__main__":
    main()
