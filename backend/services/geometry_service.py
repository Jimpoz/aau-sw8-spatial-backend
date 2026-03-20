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


def find_shared_edge_midpoint(
    poly_a: list[list[float]], poly_b: list[list[float]]
) -> Optional[tuple[float, float]]:
    """Return the midpoint of the shared boundary between two polygons.

    Falls back to the midpoint of the closest points if no shared edge exists.
    Returns None if Shapely is not available.
    """
    if not _SHAPELY:
        return None
    from shapely.ops import nearest_points

    a = ShapelyPolygon(poly_a)
    b = ShapelyPolygon(poly_b)
    shared = a.boundary.intersection(b.boundary)
    if not shared.is_empty:
        c = shared.centroid
        return (c.x, c.y)
    # Fallback: midpoint between closest points on the two polygons
    p1, p2 = nearest_points(a, b)
    return ((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)


def distance_m(cx1: float, cy1: float, cx2: float, cy2: float) -> float:
    """Euclidean distance between two points in the local coordinate system (meters)."""
    return math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2)


_WALKING_SPEED = 1.4  # m/s


def compute_traversal_cost(
    space_type: str,
    width_m: Optional[float],
    length_m: Optional[float],
    transition_time_s: Optional[float],
) -> float:
    """Return traversal cost in seconds for a Space node."""
    st = space_type.upper()

    # Connection node types — small fixed costs
    if st == "DOOR_STANDARD":
        return 1.0
    if st == "DOOR_AUTOMATIC":
        return 0.5
    if st == "DOOR_LOCKED":
        return 5.0
    if st == "DOOR_EMERGENCY":
        return 2.0
    if st == "PASSAGE":
        return 0.5

    # Vertical transport
    if st == "ELEVATOR":
        return transition_time_s or 30.0
    if st == "STAIRCASE":
        return transition_time_s or 15.0
    if st == "ESCALATOR":
        return transition_time_s or 20.0
    if st == "RAMP":
        return transition_time_s or 10.0

    # Rooms/corridors — half-diagonal at walking speed
    if width_m is not None and length_m is not None and (width_m > 0 or length_m > 0):
        half_diag = math.sqrt(width_m ** 2 + length_m ** 2) / 2.0
        return half_diag / _WALKING_SPEED

    return 1.0  # default if no dimensions
