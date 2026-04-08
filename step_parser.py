"""
step_parser.py — Parse STEP (ISO 10303-21) files to extract component metadata.

STEP files are plain ASCII. We extract:
  - Product name from PRODUCT(...) records
  - Bounding box from CARTESIAN_POINT XYZ coordinates (min/max extents)

Bounding box is returned in SVG pixels (3.78 px per mm).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MM_TO_PX = 3.78
MAX_POINTS = 5_000  # cap to avoid pathological large files
DEFAULT_BBOX = {"width": 38.0, "height": 38.0}  # ~10mm × 10mm in px

_PRODUCT_RE = re.compile(
    r"PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'",
    re.IGNORECASE,
)
_CARTESIAN_RE = re.compile(
    r"CARTESIAN_POINT\s*\([^,]*,\s*\(\s*"
    r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*,\s*"
    r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*,\s*"
    r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*\)\s*\)",
    re.IGNORECASE,
)


@dataclass
class StepInfo:
    product_name: str
    bounding_box: dict  # {"width": float, "height": float} in SVG px
    cartesian_points: list[tuple[float, float, float]] = field(default_factory=list)


def parse_step_file(step_path: Path) -> StepInfo:
    """
    Parse a STEP file and return product name + bounding box in SVG pixels.
    Falls back to DEFAULT_BBOX if no geometry is found.
    """
    text = step_path.read_text(encoding="utf-8", errors="replace")

    # Extract product name (first match, first capture group = instance name)
    product_name = step_path.stem  # fallback to filename
    match = _PRODUCT_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            product_name = candidate

    # Collect Cartesian points up to MAX_POINTS
    points: list[tuple[float, float, float]] = []
    for m in _CARTESIAN_RE.finditer(text):
        if len(points) >= MAX_POINTS:
            break
        try:
            points.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
        except ValueError:
            continue

    if not points:
        return StepInfo(product_name=product_name, bounding_box=dict(DEFAULT_BBOX))

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    width_mm = max(xs) - min(xs)
    height_mm = max(ys) - min(ys)

    # Clamp to sensible minimum (1mm) to avoid zero-size boxes
    width_px = max(width_mm, 1.0) * MM_TO_PX
    height_px = max(height_mm, 1.0) * MM_TO_PX

    return StepInfo(
        product_name=product_name,
        bounding_box={"width": round(width_px, 1), "height": round(height_px, 1)},
        cartesian_points=points,
    )


def get_bounding_box_for_part(project: "object", part_name: str) -> dict | None:
    """
    Look for <part_name>.step or <part_name>.stp (case-insensitive) in step_dir.
    Returns {"width": float, "height": float} or None if no file found.
    """
    if not project.step_dir.exists():
        return None

    name_lower = part_name.lower()
    for p in project.step_dir.iterdir():
        if p.suffix.lower() in {".step", ".stp"} and p.stem.lower() == name_lower:
            try:
                info = parse_step_file(p)
                return info.bounding_box
            except Exception:
                return None
    return None
