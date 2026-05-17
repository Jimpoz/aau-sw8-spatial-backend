import math
from typing import Optional, Tuple

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
    poly_a: list[list[float]], poly_b: list[list[float]], eps: float = 0.30
) -> Optional[tuple[float, float]]:
    """Return the midpoint of the *overlapping slice* of the two rooms'
    walls — the middle of the shorter side they actually share.
    Returns None if Shapely is not available.
    """
    if not _SHAPELY:
        return None
    from shapely.ops import nearest_points

    a = ShapelyPolygon(poly_a)
    b = ShapelyPolygon(poly_b)
    shared = a.boundary.intersection(b.boundary.buffer(eps))

    def _line_components(geom, out):
        if geom.is_empty:
            return
        if hasattr(geom, "geoms"):
            for g in geom.geoms:
                _line_components(g, out)
        elif geom.geom_type in ("LineString", "LinearRing"):
            out.append(geom)

    lines: list = []
    _line_components(shared, lines)
    if lines:
        longest = max(lines, key=lambda g: g.length)
        mid = longest.interpolate(0.5, normalized=True)
        return (mid.x, mid.y)

    # No detectable overlap — fall back to midpoint of the closest points.
    p1, p2 = nearest_points(a, b)
    return ((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)


def distance_m(cx1: float, cy1: float, cx2: float, cy2: float) -> float:
    """Euclidean distance between two points in the local coordinate system (meters)."""
    return math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2)


def local_to_global_coordinates(
    local_x: float,
    local_y: float,
    origin_lat: float,
    origin_lng: float,
    bearing: float = 0.0,
    scale: float = 1.0
) -> Tuple[float, float]:
    """
    Convert local coordinates (meters) to global lat/lng coordinates.

    Args:
        local_x: X coordinate in meters relative to building origin
        local_y: Y coordinate in meters relative to building origin
        origin_lat: Building origin latitude in degrees
        origin_lng: Building origin longitude in degrees
        bearing: Building bearing/orientation in degrees (0 = North)
        scale: Uniform scale factor applied to local coords before projection

    Returns:
        Tuple of (latitude, longitude) in degrees
    """
    # Earth's radius in meters
    EARTH_RADIUS = 6371000.0

    # Convert bearing to radians
    bearing_rad = math.radians(bearing)

    # Apply scale, then rotate coordinates by bearing
    local_x = local_x * scale
    local_y = local_y * scale
    rotated_x = local_x * math.cos(bearing_rad) - local_y * math.sin(bearing_rad)
    rotated_y = local_x * math.sin(bearing_rad) + local_y * math.cos(bearing_rad)

    # Convert to lat/lng using approximation
    lat_offset = rotated_y / 111000.0
    lng_offset = rotated_x / (111000.0 * math.cos(math.radians(origin_lat)))

    new_lat = origin_lat + lat_offset
    new_lng = origin_lng + lng_offset

    return new_lat, new_lng


def apply_edit_transform(
    lat: float,
    lng: float,
    pivot_lat: float,
    pivot_lng: float,
    delta_lat: float,
    delta_lng: float,
    delta_bearing_deg: float,
    scale_mult: float,
) -> Tuple[float, float]:
    m_per_deg_lat = 111_000.0
    m_per_deg_lng = 111_000.0 * math.cos(math.radians(pivot_lat))
    dx = (lng - pivot_lng) * m_per_deg_lng
    dy = (lat - pivot_lat) * m_per_deg_lat
    b = math.radians(delta_bearing_deg)
    rx = dx * math.cos(b) - dy * math.sin(b)
    ry = dx * math.sin(b) + dy * math.cos(b)
    sx = rx * scale_mult
    sy = ry * scale_mult
    new_lng = pivot_lng + delta_lng + sx / m_per_deg_lng
    new_lat = pivot_lat + delta_lat + sy / m_per_deg_lat
    return new_lat, new_lng


def global_to_local_coordinates(
    global_lat: float,
    global_lng: float,
    origin_lat: float,
    origin_lng: float,
    bearing: float = 0.0
) -> Tuple[float, float]:
    """
    Convert global lat/lng coordinates to local coordinates (meters).

    Args:
        global_lat: Global latitude in degrees
        global_lng: Global longitude in degrees
        origin_lat: Building origin latitude in degrees
        origin_lng: Building origin longitude in degrees
        bearing: Building bearing/orientation in degrees (0 = North)

    Returns:
        Tuple of (local_x, local_y) in meters
    """
    # Convert to meters
    lat_diff = global_lat - origin_lat
    lng_diff = global_lng - origin_lng

    # Convert to meters
    y_meters = lat_diff * 111000.0
    x_meters = lng_diff * 111000.0 * math.cos(math.radians(origin_lat))

    # Rotate back by negative bearing
    bearing_rad = math.radians(bearing)
    local_x = x_meters * math.cos(-bearing_rad) - y_meters * math.sin(-bearing_rad)
    local_y = x_meters * math.sin(-bearing_rad) + y_meters * math.cos(-bearing_rad)

    return local_x, local_y


def polygon_local_to_global(
    polygon: list[list[float]],
    origin_lat: float,
    origin_lng: float,
    bearing: float = 0.0,
    scale: float = 1.0
) -> list[list[float]]:
    """
    Convert a polygon from local coordinates to global lat/lng coordinates.

    Args:
        polygon: List of [x, y] coordinates in local meters
        origin_lat: Building origin latitude
        origin_lng: Building origin longitude
        bearing: Building bearing in degrees
        scale: Uniform scale factor applied to local coords before projection

    Returns:
        List of [lat, lng] coordinates
    """
    return [
        list(local_to_global_coordinates(x, y, origin_lat, origin_lng, bearing, scale))
        for x, y in polygon
    ]


def polygon_global_to_local(
    polygon: list[list[float]],
    origin_lat: float,
    origin_lng: float,
    bearing: float = 0.0
) -> list[list[float]]:
    """
    Convert a polygon from global lat/lng coordinates to local coordinates.

    Args:
        polygon: List of [lat, lng] coordinates
        origin_lat: Building origin latitude
        origin_lng: Building origin longitude
        bearing: Building bearing in degrees

    Returns:
        List of [x, y] coordinates in meters
    """
    return [
        list(global_to_local_coordinates(lat, lng, origin_lat, origin_lng, bearing))
        for lat, lng in polygon
    ]


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
