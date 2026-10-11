#!/usr/bin/env python3
"""Download and mosaic Esri World Imagery for the bounds of an OSM file."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


PUBLIC_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TOKEN_TILE_URL = "https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}?token={token}"
ATTRIBUTION = "Esri, Vantor, Earthstar Geographics, and the GIS User Community"
MAX_TILES = 64


def osm_bounds(path: Path) -> tuple[float, float, float, float]:
    root = ET.parse(path).getroot()
    bounds = root.find("bounds")
    if bounds is not None:
        return (
            float(bounds.get("minlon", "")), float(bounds.get("minlat", "")),
            float(bounds.get("maxlon", "")), float(bounds.get("maxlat", "")),
        )
    coordinates = [
        (float(node.get("lon", "")), float(node.get("lat", "")))
        for node in root.findall("node")
        if node.get("lon") is not None and node.get("lat") is not None
    ]
    if not coordinates:
        raise ValueError("OSM 文件没有 bounds，也没有可用于计算范围的节点。")
    lons, lats = zip(*coordinates)
    return min(lons), min(lats), max(lons), max(lats)


def lon_to_x(lon: float, zoom: int) -> int:
    n = 1 << zoom
    return max(0, min(n - 1, int((lon + 180.0) / 360.0 * n)))


def lat_to_y(lat: float, zoom: int) -> int:
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 1 << zoom
    radians = math.radians(lat)
    value = (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * n
    return max(0, min(n - 1, int(value)))


def plan_for_osm(path: Path, zoom: int = 18) -> dict:
    min_lon, min_lat, max_lon, max_lat = osm_bounds(path)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("OSM 范围无效。")
    x_min, x_max = lon_to_x(min_lon, zoom), lon_to_x(max_lon, zoom)
    y_min, y_max = lat_to_y(max_lat, zoom), lat_to_y(min_lat, zoom)
    width, height = x_max - x_min + 1, y_max - y_min + 1
    return {
        "x": x_min, "y": y_min, "z": zoom,
        "tiles_wide": width, "tiles_high": height,
        "tile_count": width * height,
        "pixel_width": width * 256, "pixel_height": height * 256,
        "osm_bounds": [min_lon, min_lat, max_lon, max_lat],
    }


def download(plan: dict, output_dir: Path) -> tuple[Path, Path]:
    if plan["tile_count"] > MAX_TILES:
        raise ValueError(
            f"范围需要 {plan['tiles_wide']}×{plan['tiles_high']}={plan['tile_count']} 张瓦片，"
            f"超过安全上限 {MAX_TILES}。请在 JOSM 中下载更小的区域。"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    mosaic = Image.new("RGB", (plan["pixel_width"], plan["pixel_height"]))
    token = os.environ.get("ESRI_API_KEY", "").strip()
    template = TOKEN_TILE_URL if token else PUBLIC_TILE_URL
    total = plan["tile_count"]
    current = 0
    for row in range(plan["tiles_high"]):
        for column in range(plan["tiles_wide"]):
            x, y, z = plan["x"] + column, plan["y"] + row, plan["z"]
            url = template.format(x=x, y=y, z=z, token=token)
            request = urllib.request.Request(url, headers={"User-Agent": "OSM-Building-Postprocess/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    tile = Image.open(io.BytesIO(response.read())).convert("RGB")
            except (OSError, urllib.error.URLError) as exc:
                raise RuntimeError(f"下载瓦片失败 z={z} x={x} y={y}: {exc}") from exc
            if tile.size != (256, 256):
                raise RuntimeError(f"瓦片尺寸异常 z={z} x={x} y={y}: {tile.size}")
            mosaic.paste(tile, (column * 256, row * 256))
            current += 1
            print(f"正在下载 {current}/{total}", flush=True)

    image_path = output_dir / "esri-imagery-z18.jpg"
    metadata_path = output_dir / "imagery-info.json"
    mosaic.save(image_path, quality=95, subsampling=0)
    metadata = {
        **plan,
        "image": image_path.name,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "service": "Esri World Imagery",
        "attribution": ATTRIBUTION,
        "service_url": PUBLIC_TILE_URL,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"卫星图：{image_path}", flush=True)
    print(f"定位信息：{metadata_path}", flush=True)
    return image_path, metadata_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Esri imagery for an OSM file's bounds")
    parser.add_argument("osm", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--zoom", type=int, default=18)
    parser.add_argument("--plan", action="store_true", help="Only print the download plan")
    args = parser.parse_args()
    try:
        plan = plan_for_osm(args.osm.resolve(), args.zoom)
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        if not args.plan:
            download(plan, args.output_dir.resolve())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
