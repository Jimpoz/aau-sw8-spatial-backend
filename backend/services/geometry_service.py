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


def distance_m(cx1: float, cy1: float, cx2: float, cy2: float) -> float:
    """Euclidean distance between two points in the local coordinate system (meters)."""
    return math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2)


def local_to_global_coordinates(
    local_x: float,
    local_y: float,
    origin_lat: float,
    origin_lng: float,
    bearing: float = 0.0
) -> Tuple[float, float]:
    """
    Convert local coordinates (meters) to global lat/lng coordinates.

    Args:
        local_x: X coordinate in meters relative to building origin
        local_y: Y coordinate in meters relative to building origin
        origin_lat: Building origin latitude in degrees
        origin_lng: Building origin longitude in degrees
        bearing: Building bearing/orientation in degrees (0 = North)

    Returns:
        Tuple of (latitude, longitude) in degrees
    """
    # Earth's radius in meters
    EARTH_RADIUS = 6371000.0

    # Convert bearing to radians
    bearing_rad = math.radians(bearing)

    # Rotate coordinates by bearing
    rotated_x = local_x * math.cos(bearing_rad) - local_y * math.sin(bearing_rad)
    rotated_y = local_x * math.sin(bearing_rad) + local_y * math.cos(bearing_rad)

    # Convert to lat/lng using approximation
    lat_offset = rotated_y / 111000.0
    lng_offset = rotated_x / (111000.0 * math.cos(math.radians(origin_lat)))

    new_lat = origin_lat + lat_offset
    new_lng = origin_lng + lng_offset

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
    bearing: float = 0.0
) -> list[list[float]]:
    """
    Convert a polygon from local coordinates to global lat/lng coordinates.

    Args:
        polygon: List of [x, y] coordinates in local meters
        origin_lat: Building origin latitude
        origin_lng: Building origin longitude
        bearing: Building bearing in degrees

    Returns:
        List of [lat, lng] coordinates
    """
    return [
        list(local_to_global_coordinates(x, y, origin_lat, origin_lng, bearing))
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
