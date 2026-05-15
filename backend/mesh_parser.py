from __future__ import annotations

import re
from pathlib import Path
from typing import Any


MESH_HEADER = "[bed_mesh MESH_DATA]"


def _to_float_row(line: str) -> list[float]:
    # Extract signed numbers to avoid dropping the first negative value.
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", line)]


def parse_bed_mesh_from_text(text: str) -> dict[str, Any] | None:
    idx = text.find(MESH_HEADER)
    if idx < 0:
        return None

    block = text[idx:].split("=======================", 1)[0]
    lines = [ln.rstrip() for ln in block.splitlines()]

    points: list[list[float]] = []
    min_x = min_y = max_x = max_y = None
    x_count = y_count = None

    in_points = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("points"):
            in_points = True
            continue

        if in_points and stripped.startswith("-"):
            points.append(_to_float_row(stripped))
            continue

        if in_points and not stripped.startswith("-"):
            in_points = False

        if stripped.startswith("x_count"):
            x_count = int(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("y_count"):
            y_count = int(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("min_x"):
            min_x = float(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("max_x"):
            max_x = float(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("min_y"):
            min_y = float(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("max_y"):
            max_y = float(stripped.split("=", 1)[1].strip())

    if not points:
        return None

    flat = [v for row in points for v in row]
    z_min = min(flat)
    z_max = max(flat)
    z_range = z_max - z_min
    z_avg = sum(flat) / len(flat)

    corners = {
        "front_left": points[0][0],
        "front_right": points[0][-1],
        "rear_left": points[-1][0],
        "rear_right": points[-1][-1],
        "avg": z_avg,
    }

    deltas = {k: round(v - z_avg, 6) for k, v in corners.items() if k != "avg"}

    return {
        "points": points,
        "x_count": x_count or len(points[0]),
        "y_count": y_count or len(points),
        "bounds": {
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
        },
        "stats": {
            "z_min": z_min,
            "z_max": z_max,
            "z_range": z_range,
            "z_avg": z_avg,
        },
        "corners": corners,
        "corner_deltas_from_avg": deltas,
    }


def parse_bed_mesh_from_file(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="ignore")
    return parse_bed_mesh_from_text(text)
