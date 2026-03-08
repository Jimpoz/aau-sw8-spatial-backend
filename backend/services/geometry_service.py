import math
from typing import Optional

try:
    from shapely.geometry import Polygon as ShapelyPolygon

    _SHAPELY = True
except ImportError:
    _SHAPELY = False


def centroid_from_polygon(polygon: list[list[float]]) -> tuple[float, float]:
    """Return (cx, cy) centroid of a polygon vertex list."""
    if _SHAPELY and len(polygon) >= 3:
        poly = ShapelyPolygon(polygon)
        c = poly.centroid
        return c.x, c.y
    # Fallback: arithmetic mean of vertices
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def area_from_polygon(polygon: list[list[float]]) -> float:
    """Return area in m² using the shoelace formula (or shapely)."""
    if _SHAPELY and len(polygon) >= 3:
        return abs(ShapelyPolygon(polygon).area)
    # Shoelace formula
    n = len(polygon)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += polygon[i][0] * polygon[j][1]
        area -= polygon[j][0] * polygon[i][1]
    return abs(area) / 2.0


def distance_m(cx1: float, cy1: float, cx2: float, cy2: float) -> float:
    """Euclidean distance between two points in the local coordinate system (meters)."""
    return math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2)


# Walking speeds in m/s for weight computation
_SPEEDS = {
    "WALKWAY": 1.4,
    "DOORWAY": 1.4,
    "OUTDOOR_PATH": 1.4,
    "BRIDGE": 1.4,
    "TUNNEL": 1.4,
    "COVERED_WALKWAY": 1.4,
    "STAIRCASE_UP": 0.5,
    "STAIRCASE_DOWN": 0.7,
    "RAMP_UP": 0.7,
    "RAMP_DOWN": 0.9,
    "ESCALATOR_UP": 0.8,
    "ESCALATOR_DOWN": 0.8,
}
_ELEVATOR_DEFAULT_S = 30.0  # seconds for elevator transition when no override given


def compute_weight(
    connection_type: str,
    dist: Optional[float],
    transition_time_s: Optional[float],
) -> float:
    """Return traversal cost in walking-second equivalents."""
    ct = connection_type.upper()
    if ct in ("ELEVATOR_UP", "ELEVATOR_DOWN"):
        return transition_time_s if transition_time_s is not None else _ELEVATOR_DEFAULT_S
    speed = _SPEEDS.get(ct, 1.4)
    d = dist if dist is not None else 1.0
    return d / speed
