"""
DXF / DWG floor-plan import. SOME ISSUES STILL
"""
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional
from collections import Counter
import re

import ezdxf
from ezdxf import recover as ezdxf_recover
from ezdxf.document import Drawing
from shapely.geometry import Point, Polygon, LineString
from shapely.validation import make_valid
from shapely.ops import polygonize, unary_union
import math


# Label keyword patterns: SpaceType -> keywords. Order matters — entries
# higher up are checked first (e.g. RESTROOM_ACCESSIBLE before RESTROOM).
# Within each entry, the per-token resolution priority is enforced by
# _PATTERN_INDEX which sorts by descending pattern length.
_LABEL_TYPE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("RESTROOM_ACCESSIBLE", ("HC", "HANDICAP")),
    ("RESTROOM", ("WC", "TOILET", "TOILETTER", "BAD", "BATH", "DAMER",
                  "HERRER", "DAME", "PUSLERUM", "LAVATORY")),
    ("ROOM_LAB", ("LABORATORIUM", "LABORATORIE", "LABORATORY", "WORKSHOP",
                  "VÆRKSTED", "VAERKSTED", "AUDIO", "MEDIA", "LAB")),
    ("ROOM_UTILITY", ("TEKNIK", "RENG", "RENGØRING", "KRYDSFELT", "TAVLE",
                      "EL-RUM", "VENTILATION", "INSTALLATION", "SERVERRUM",
                      "FORDELER", "MASKINRUM", "PUMPERUM", "UTILITY")),
    ("ROOM_STORAGE", ("LAGER", "STORAGE", "DEPOT", "ARKIV", "ARKIVRUM",
                      "REDSKAB", "MATERIEL", "GARDEROBE", "CYKEL")),
    ("ROOM_LECTURE_HALL", ("AUDITORIUM", "AUDITORIA", "AUDITORIE",
                           "FORELÆSNING", "FORELAESNING", "AUD")),
    ("ROOM_CLASSROOM", ("SEMINARRUM", "SEMINAR", "KLASSE", "CLASSROOM",
                        "UNDERVISNING", "GRUPPERUM", "STUDIE")),
    ("ROOM_MEETING", ("MØDE", "MOEDE", "MEETING", "KONFERENCE",
                      "CONFERENCE", "BOARDROOM")),
    ("ROOM_OFFICE", ("KONTOR", "OFFICE", "OFC", "ARBEJDSRUM", "TEAMRUM")),
    ("LOBBY", ("FORRUM", "FOYER", "LOBBY", "VESTIBULE", "SLUSE")),
    ("CORRIDOR", ("GANGAREAL", "TVÆRGANG", "GANG", "CORRIDOR", "HALLWAY",
                  "PASSAGE", "HALL")),
    ("ENTRANCE", ("HOVEDINDGANG", "BAGINDGANG", "INDGANGSPARTI", "INDGANG",
                  "ENTRANCE", "ENTRY")),
    ("CAFETERIA", ("KANTINE", "CAFETERIA", "KITCHEN", "DINING", "FROKOSTRUM")),
    ("CAFE", ("CAFE", "CAFÉ", "PAUSEZONE", "TEKØKKEN", "TEKOEKKEN")),
    ("RECEPTION", ("RECEPTION", "INFO")),
    ("STAIRCASE", ("TRAPPEHUS", "NØDTRAPPE", "STAIR", "TRAPPE")),
    ("ELEVATOR", ("ELEVATOR", "ELEV", "LIFT")),
    ("LIBRARY", ("BIBLIOTEK", "LIBRARY")),
    ("SHOP", ("SHOP", "STORE", "RETAIL")),
]

# Flat (pattern, space_type) list sorted by descending pattern length so
# SEMINARRUM matches before SEMINAR, GRUPPERUM before GRUPPE, etc.
_PATTERN_INDEX: list[tuple[str, str]] = sorted(
    [(p, t) for t, patterns in _LABEL_TYPE_PATTERNS for p in patterns],
    key=lambda kv: len(kv[0]),
    reverse=True,
)

_NAVIGABLE_TYPES = frozenset({
    "CORRIDOR", "LOBBY", "ENTRANCE", "STAIRCASE", "ELEVATOR",
    "RECEPTION", "PASSAGE",
})

# DXF $INSUNITS code -> meters per unit (subset; from DXF spec).
_INSUNITS_TO_METERS: dict[int, float] = {
    1: 0.0254,   # inches
    2: 0.3048,   # feet
    4: 0.001,    # millimeters
    5: 0.01,     # centimeters
    6: 1.0,      # meters
    14: 1e-7,    # decimicrons
    21: 0.1,     # decimeters
}

_MIN_ROOM_AREA = 0.5                  # raw-unit early degenerate-polygon filter
_MIN_ROOM_AREA_M2 = 1.5               # post-scaling
_MAX_ROOM_AREA_M2 = 500.0             # rooms larger than this are envelopes/courtyards
_LABEL_BOUNDARY_BUFFER_M = 0.6        # narrow tolerance for "label on the wall"
_NEAREST_LABEL_FALLBACK_M = 3.0       # leader-line / external-label adoption (after primary attach fails)
_SMALL_UNLABELED_DROP_AREA_M2 = 5.0   # unlabeled polys smaller than this are dropped
_MIN_ROOM_AREA_FOR_LABEL_M2 = 2.0     # below this, a polygon won't steal a contained label
_DOOR_ARC_RADIUS_RANGE_M = (0.4, 1.5)
_DOOR_ARC_SWEEP_DEG = (45.0, 130.0)
_DOOR_CONNECTION_THRESHOLD_M = 0.4

_LAT_RANGE = (-90.0, 90.0)
_LNG_RANGE = (-180.0, 180.0)
_POLYGONIZE_SNAP_TOLERANCE = 1e-3
_OUTER_AREA_RATIO = 8.0
# DXF FILE LIMITS
_MAX_DXF_BYTES = 80 * 1024 * 1024
_MAX_FINAL_ROOMS = 8000
_MAX_TEXT_ENTITIES = 5000


def _classify_label(text: Optional[str]) -> tuple[str, float]:
    """Match a label / layer name against `_PATTERN_INDEX` and return
    `(space_type, confidence)`.

    Whole-word match for short keywords (<= 3 chars) so abbreviations
    like "HC" or "WC" do not substring-match unrelated tokens; substring
    match for longer keywords. Patterns are length-sorted so SEMINARRUM
    wins over SEMINAR, GRUPPERUM over GRUPPE, etc.
    """
    if not text:
        return "ROOM_GENERIC", 0.4
    upper = text.upper()
    for pattern, space_type in _PATTERN_INDEX:
        if len(pattern) <= 3:
            if re.search(rf"\b{re.escape(pattern)}\b", upper):
                return space_type, 0.85
        elif pattern in upper:
            return space_type, 0.9
    return "ROOM_GENERIC", 0.4


def _classify_layer(
    layer_name: Optional[str],
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """Legacy shim: classifies by layer name only (no label).
    Retained for callers that only have a layer name handy.
    """
    if overrides and layer_name:
        for k, v in overrides.items():
            if k and k.upper() == layer_name.upper():
                return v
    space_type, _ = _classify_label(layer_name)
    return space_type


def _resolve_unit_scale(
    doc: Drawing,
    polygon_bbox_span: Optional[float] = None,
) -> tuple[float, list[str]]:
    """Return `(meters_per_unit, warnings)`.

    Reads `$INSUNITS` from the DXF header. When the unit code is 0
    (unitless), falls back to a bounding-box heuristic — drawings whose
    extent is in the thousands are almost certainly mm-based.
    """
    warnings: list[str] = []
    code = 0
    try:
        header = getattr(doc, "header", None)
        if header is not None:
            code = int(header.get("$INSUNITS", 0))
    except Exception:
        code = 0

    scale = _INSUNITS_TO_METERS.get(code)
    if scale is not None:
        return scale, [f"DXF $INSUNITS={code} -> {scale} m/unit"]

    if polygon_bbox_span is None or polygon_bbox_span <= 0:
        warnings.append(
            "DXF $INSUNITS=0 (unitless) and no polygon bbox available; assuming meters."
        )
        return 1.0, warnings
    if polygon_bbox_span > 5000:
        warnings.append(
            f"DXF $INSUNITS=0; bbox span {polygon_bbox_span:.0f} suggests millimeters (x0.001)."
        )
        return 0.001, warnings
    if polygon_bbox_span > 200:
        warnings.append(
            f"DXF $INSUNITS=0; bbox span {polygon_bbox_span:.0f} suggests centimeters (x0.01)."
        )
        return 0.01, warnings
    warnings.append(
        f"DXF $INSUNITS=0; bbox span {polygon_bbox_span:.0f} suggests meters (x1.0)."
    )
    return 1.0, warnings


def _scale_rooms(rooms: list[dict], scale_m: float) -> None:
    """Scale all room polygons in place from raw units to meters."""
    if scale_m == 1.0:
        return
    for r in rooms:
        poly = r.get("polygon") or []
        r["polygon"] = [(float(x) * scale_m, float(y) * scale_m) for x, y in poly]


def _scale_texts(texts: list[dict], scale_m: float) -> None:
    """Scale text insertion points in place from raw units to meters."""
    if scale_m == 1.0:
        return
    for t in texts:
        try:
            t["x"] = float(t["x"]) * scale_m
            t["y"] = float(t["y"]) * scale_m
        except Exception:
            continue


def _polygon_area_m2(coords) -> float:
    """Best-effort polygon area; returns 0 on construction failure."""
    try:
        return float(Polygon(coords).area)
    except Exception:
        return 0.0


def _polygon_from_lwpolyline(entity) -> Optional[list[tuple[float, float]]]:
    if not entity.closed:
        return None
    coords = [(float(pt[0]), float(pt[1])) for pt in entity.get_points()]
    return coords if len(coords) >= 3 else None


def _polygon_from_polyline(entity) -> Optional[list[tuple[float, float]]]:
    if not getattr(entity, "is_closed", False):
        return None
    try:
        coords = [
            (float(v.dxf.location[0]), float(v.dxf.location[1]))
            for v in entity.vertices
        ]
    except Exception:
        return None
    return coords if len(coords) >= 3 else None


def _walk_polygons(entities, depth: int = 0, inherited_layer: Optional[str] = None) -> list[dict]:
    """Recursive walk over an entity collection — modelspace, or the
    virtual children of an INSERT — collecting closed polylines as
    `{layer, polygon}` dicts.

    `inherited_layer` is used so children inside an INSERT that are on
    layer "0" inherit the parent INSERT's layer for classification
    purposes.
    """
    out: list[dict] = []
    if depth > 6:
        return out
    for entity in entities:
        try:
            dxftype = entity.dxftype()
        except Exception:
            continue
        eff_layer = None
        try:
            raw_layer = getattr(entity.dxf, "layer", None)
            eff_layer = raw_layer if (raw_layer and str(raw_layer).upper() != "0") else inherited_layer
        except Exception:
            eff_layer = inherited_layer

        if dxftype == "LWPOLYLINE":
            poly = _polygon_from_lwpolyline(entity)
            if poly:
                out.append({"layer": eff_layer or (getattr(entity.dxf, "layer", "") or ""), "polygon": poly, "source": "closed_polyline"})
        elif dxftype == "POLYLINE":
            poly = _polygon_from_polyline(entity)
            if poly:
                out.append({"layer": eff_layer or (getattr(entity.dxf, "layer", "") or ""), "polygon": poly, "source": "closed_polyline"})
        elif dxftype == "INSERT":
            try:
                child_layer = getattr(entity.dxf, "layer", None) or inherited_layer
                out.extend(_walk_polygons(entity.virtual_entities(), depth + 1, inherited_layer=child_layer))
            except Exception:
                continue
    return out


def _approximate_arc_points(center_x: float, center_y: float, radius: float, start_deg: float, end_deg: float, steps: int = 16) -> list[tuple[float, float]]:
    """Approximate an ARC/CIRCLE by sampling points along the circular arc."""
    # Normalize angles
    start = float(start_deg) % 360.0
    end = float(end_deg) % 360.0
    if end <= start:
        end += 360.0
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = start + (end - start) * (i / steps)
        rad = math.radians(t)
        x = center_x + radius * math.cos(rad)
        y = center_y + radius * math.sin(rad)
        points.append((float(x), float(y)))
    return points


def _collect_segments_from_entities(entities, depth: int = 0) -> list[LineString]:
    """Collect linear segments from an entity iterable (modelspace or
    virtual_entities). Recurses into INSERTs. Returns raw LineString
    segments (coordinates in document units)."""
    segments: list[LineString] = []
    if depth > 8:
        return segments
    for e in entities:
        try:
            t = e.dxftype()
        except Exception:
            continue
        try:
            if t == "LINE":
                s = e.dxf.start
                ed = e.dxf.end
                segments.append(LineString([(float(s[0]), float(s[1])), (float(ed[0]), float(ed[1]))]))
            elif t == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
                if len(pts) >= 2:
                    for a, b in zip(pts, pts[1:]):
                        segments.append(LineString([a, b]))
                    if getattr(e, "closed", False) and pts[0] != pts[-1]:
                        segments.append(LineString([pts[-1], pts[0]]))
            elif t == "POLYLINE":
                try:
                    pts = [(float(v.dxf.location[0]), float(v.dxf.location[1])) for v in e.vertices]
                except Exception:
                    pts = []
                if len(pts) >= 2:
                    for a, b in zip(pts, pts[1:]):
                        segments.append(LineString([a, b]))
                    if getattr(e, "is_closed", False) and pts[0] != pts[-1]:
                        segments.append(LineString([pts[-1], pts[0]]))
            elif t == "CIRCLE":
                c = e.dxf.center
                r = float(e.dxf.radius)
                pts = _approximate_arc_points(float(c[0]), float(c[1]), r, 0.0, 360.0, steps=32)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
            elif t == "ARC":
                c = e.dxf.center
                r = float(e.dxf.radius)
                start = float(e.dxf.start_angle)
                end = float(e.dxf.end_angle)
                pts = _approximate_arc_points(float(c[0]), float(c[1]), r, start, end, steps=20)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
            elif t == "INSERT":
                try:
                    segments.extend(_collect_segments_from_entities(e.virtual_entities(), depth + 1))
                except Exception:
                    continue
        except Exception:
            continue
    return segments


def _snap_and_dedupe_segments(segments: list[LineString], snap_tol: float) -> list[LineString]:
    """Snap segment endpoints to a coarse grid (precision snap_tol),
    dedupe identical/degenerate segments, and return cleaned
    LineStrings suitable for polygonization.
    This is intentionally conservative: snap_tol should be small for
    precise results.
    """
    if not segments:
        return []

    cell_map: dict[tuple[int, int], list[tuple[float, float]]] = {}
    pts_keys: list[tuple[int, int]] = []
    for s in segments:
        try:
            coords = list(s.coords)
        except Exception:
            continue
        for x, y in (coords[0], coords[-1]):
            key = (int(round(x / snap_tol)), int(round(y / snap_tol)))
            cell_map.setdefault(key, []).append((float(x), float(y)))
            pts_keys.append(key)

    canonical: dict[tuple[int, int], tuple[float, float]] = {}
    for k, pts in cell_map.items():
        sx = sum(p[0] for p in pts)
        sy = sum(p[1] for p in pts)
        n = len(pts)
        canonical[k] = (sx / n, sy / n)

    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    out: list[LineString] = []
    for s in segments:
        try:
            coords = list(s.coords)
            a = coords[0]
            b = coords[-1]
            ka = (int(round(a[0] / snap_tol)), int(round(a[1] / snap_tol)))
            kb = (int(round(b[0] / snap_tol)), int(round(b[1] / snap_tol)))
            pa = canonical.get(ka, (float(a[0]), float(a[1])))
            pb = canonical.get(kb, (float(b[0]), float(b[1])))
            if pa == pb:
                continue
            key = (pa, pb) if pa <= pb else (pb, pa)
            if key in seen:
                continue
            seen.add(key)
            out.append(LineString([pa, pb]))
        except Exception:
            continue

    return out


def _polygonize_from_doc(
    doc: Drawing,
    *,
    snap_tolerance_raw: Optional[float] = None,
    max_segments: int = 200_000,
) -> tuple[list[dict], list[str]]:
    """Polygonize by collecting segments (recursing into INSERTs),
    snapping endpoints, merging, and running `shapely.ops.polygonize`.
    Returns `(rooms, warnings)`.

    `snap_tolerance_raw` is the endpoint-snapping tolerance in raw DXF
    units. When unset, falls back to the historical bbox-based heuristic
    (which is too tight for most CAD floor plans). Pass an explicit
    physical-distance value (e.g. 20mm equivalent) for reliable noding.
    """
    warnings: list[str] = []
    try:
        msp = doc.modelspace()
    except Exception:
        return [], warnings

    raw_segments = _collect_segments_from_entities(msp)
    if not raw_segments:
        return [], warnings
    if len(raw_segments) > max_segments:
        warnings.append(
            f"polygonize: segment count {len(raw_segments)} exceeds cap {max_segments}; skipped."
        )
        return [], warnings

    if snap_tolerance_raw is not None:
        snap_tol = float(snap_tolerance_raw)
    else:
        try:
            xs = [c for s in raw_segments for c in (s.coords[0][0], s.coords[-1][0])]
            ys = [c for s in raw_segments for c in (s.coords[0][1], s.coords[-1][1])]
            if xs and ys:
                dx = max(xs) - min(xs)
                dy = max(ys) - min(ys)
                diag = math.hypot(dx, dy)
                snap_tol = max(_POLYGONIZE_SNAP_TOLERANCE, diag * 1e-6)
            else:
                snap_tol = _POLYGONIZE_SNAP_TOLERANCE
        except Exception:
            snap_tol = _POLYGONIZE_SNAP_TOLERANCE

    segs = _snap_and_dedupe_segments(raw_segments, snap_tol)
    if not segs:
        return [], warnings

    try:
        merged = unary_union(segs)
        polys = list(polygonize(merged))
    except Exception as exc:
        warnings.append(f"polygonize: shapely failed: {exc}")
        return [], warnings

    polys = [p for p in polys if not p.is_empty and p.area >= _MIN_ROOM_AREA]
    if not polys:
        return [], warnings

    areas = sorted([p.area for p in polys])
    if len(areas) >= 2:
        median = areas[len(areas) // 2]
        largest = max(areas)
        if median > 0 and largest / median > _OUTER_AREA_RATIO:
            largest_poly = max(polys, key=lambda p: p.area)
            contains_count = sum(1 for p in polys if largest_poly.buffer(0).contains(p) and p != largest_poly)
            if contains_count >= max(1, len(polys) // 2):
                polys = [p for p in polys if p != largest_poly]

    out: list[dict] = []
    for poly in polys:
        coords = list(poly.exterior.coords)
        out.append({"layer": "", "polygon": coords, "source": "polygonized_linework"})
    return out, warnings


def _polygonize_from_parsed(parsed: dict) -> list[dict]:
    """Construct line segments from the `dxf-json` parsed dict and
    polygonize them to find enclosed polygons."""
    entities = parsed.get("entities") or []
    segments: list[LineString] = []
    for e in entities:
        t = (e.get("type") or "").upper()
        try:
            if t == "LINE":
                # Various field names may be used by dxf-json
                a = e.get("start") or e.get("startPoint") or e.get("from") or e.get("p1")
                b = e.get("end") or e.get("endPoint") or e.get("to") or e.get("p2")
                if not a or not b:
                    continue
                def _pt(x):
                    if isinstance(x, dict):
                        return float(x.get("x")), float(x.get("y"))
                    elif isinstance(x, (list, tuple)) and len(x) >= 2:
                        return float(x[0]), float(x[1])
                    return None
                pa = _pt(a)
                pb = _pt(b)
                if pa and pb:
                    segments.append(LineString([pa, pb]))
            elif t in ("LWPOLYLINE", "POLYLINE"):
                verts = e.get("vertices") or []
                pts = []
                for v in verts:
                    if isinstance(v, dict):
                        x = v.get("x")
                        y = v.get("y")
                    elif isinstance(v, (list, tuple)) and len(v) >= 2:
                        x, y = v[0], v[1]
                    else:
                        continue
                    try:
                        pts.append((float(x), float(y)))
                    except Exception:
                        continue
                if len(pts) >= 2:
                    for a, b in zip(pts, pts[1:]):
                        segments.append(LineString([a, b]))
                    flag = e.get("flag") or e.get("flags") or 0
                    closed = False
                    try:
                        if int(flag) & 1:
                            closed = True
                    except Exception:
                        closed = False
                    if closed and pts[0] != pts[-1]:
                        segments.append(LineString([pts[-1], pts[0]]))
            elif t == "CIRCLE":
                c = e.get("center") or e.get("centerPoint") or e.get("position")
                r = e.get("radius")
                if not c or not r:
                    continue
                try:
                    cx = float(c.get("x")) if isinstance(c, dict) else float(c[0])
                    cy = float(c.get("y")) if isinstance(c, dict) else float(c[1])
                    rr = float(r)
                except Exception:
                    continue
                pts = _approximate_arc_points(cx, cy, rr, 0.0, 360.0, steps=24)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
            elif t == "ARC":
                c = e.get("center") or e.get("centerPoint")
                r = e.get("radius")
                start = e.get("startAngle") or e.get("start") or 0
                end = e.get("endAngle") or e.get("end") or 0
                if not c or not r:
                    continue
                try:
                    cx = float(c.get("x")) if isinstance(c, dict) else float(c[0])
                    cy = float(c.get("y")) if isinstance(c, dict) else float(c[1])
                    rr = float(r)
                    st = float(start)
                    ed = float(end)
                except Exception:
                    continue
                pts = _approximate_arc_points(cx, cy, rr, st, ed, steps=12)
                for a, b in zip(pts, pts[1:]):
                    segments.append(LineString([a, b]))
        except Exception:
            continue

    if not segments:
        return []

    try:
        merged = unary_union(segments)
        polys = list(polygonize(merged))
    except Exception:
        return []

    out: list[dict] = []
    for poly in polys:
        if poly.is_empty:
            continue
        if poly.area < _MIN_ROOM_AREA:
            continue
        coords = list(poly.exterior.coords)
        out.append({"layer": "", "polygon": coords})
    return out


def _validate_and_dedupe(rooms: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop zero-area / invalid polygons, attempt automatic repair on
    polygons shapely flags as invalid, and dedupe coincident polygons.
    Returns the cleaned list plus a human-readable warning trail the
    response can surface to the editor."""
    warnings: list[str] = []
    seen_signatures: set[tuple] = set()
    cleaned: list[dict] = []
    for r in rooms:
        polygon = r["polygon"]
        try:
            shape = Polygon(polygon)
        except Exception:
            warnings.append(f"layer {r['layer']!r}: polygon failed to construct, dropped")
            continue
        if not shape.is_valid:
            try:
                repaired = make_valid(shape)
                if repaired.geom_type == "MultiPolygon":
                    parts = list(repaired.geoms)
                    parts.sort(key=lambda g: g.area, reverse=True)
                    shape = parts[0] if parts else None
                elif repaired.geom_type == "Polygon":
                    shape = repaired
                else:
                    shape = None
                if shape is None or shape.is_empty:
                    warnings.append(f"layer {r['layer']!r}: polygon could not be repaired, dropped")
                    continue
                warnings.append(f"layer {r['layer']!r}: polygon was self-intersecting and was auto-repaired")
                # Replace the raw vertex list with the repaired shell.
                polygon = list(shape.exterior.coords)
                r = {**r, "polygon": polygon}
            except Exception:
                warnings.append(f"layer {r['layer']!r}: polygon was invalid and could not be repaired, dropped")
                continue
        if shape.area < _MIN_ROOM_AREA:
            continue
        sig = tuple(sorted((round(x, 4), round(y, 4)) for x, y in polygon))
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        cleaned.append(r)
    return cleaned, warnings


def _extract_texts(doc: Drawing) -> list[dict]:
    """Collect text-like labels from the document."""
    def _clean_mtext(raw: str) -> str:
        s = str(raw)
        
        s = s.replace("\r", " ").replace("\n", " ")
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"\{\\.*?\}", "", s)
        return s.strip()

    texts: list[dict] = []

    def _walk_text_entities(entities, inherited_layer: Optional[str] = None):
        for e in entities:
            try:
                t = e.dxftype()
            except Exception:
                continue
            eff_layer = None
            try:
                raw_layer = getattr(e.dxf, "layer", None)
                eff_layer = raw_layer if (raw_layer and str(raw_layer).upper() != "0") else inherited_layer
            except Exception:
                eff_layer = inherited_layer

            if t == "TEXT":
                try:
                    insert = e.dxf.insert
                    raw = e.text if hasattr(e, "text") else e.dxf.text
                    if raw:
                        texts.append({"x": float(insert[0]), "y": float(insert[1]), "text": str(raw).strip(), "layer": eff_layer or "", "source_type": "TEXT"})
                except Exception:
                    continue
            elif t == "MTEXT":
                try:
                    insert = e.dxf.insert if hasattr(e.dxf, "insert") else (e.dxf.insert if hasattr(e.dxf, "insert") else None)
                    raw = e.text if hasattr(e, "text") else getattr(e.dxf, "text", "")
                    if raw and insert:
                        texts.append({"x": float(insert[0]), "y": float(insert[1]), "text": _clean_mtext(raw), "layer": eff_layer or "", "source_type": "MTEXT"})
                except Exception:
                    continue
            elif t == "ATTRIB":
                try:
                    insert = e.dxf.insert
                    raw = e.dxf.text if hasattr(e.dxf, "text") else getattr(e, "text", "")
                    if raw:
                        texts.append({"x": float(insert[0]), "y": float(insert[1]), "text": str(raw).strip(), "layer": eff_layer or "", "source_type": "ATTRIB"})
                except Exception:
                    continue
            elif t == "INSERT":
                try:
                    child_layer = getattr(e.dxf, "layer", None) or inherited_layer
                    _walk_text_entities(e.virtual_entities(), inherited_layer=child_layer)
                except Exception:
                    continue

    _walk_text_entities(doc.modelspace(), inherited_layer=None)
    return texts


def _attach_labels(rooms: list[dict], texts: list[dict]) -> None:
    polys = []
    for r in rooms:
        try:
            polys.append((r, Polygon(r["polygon"])))
        except Exception:
            polys.append((r, None))

    # Collect labels per room
    labels_per_room: dict[int, list[dict]] = {i: [] for i in range(len(rooms))}
    for t in texts:
        p = Point(t["x"], t["y"])
        for idx, (r, poly) in enumerate(polys):
            if poly is None or not poly.is_valid:
                continue
            try:
                if poly.contains(p):
                    labels_per_room[idx].append(t)
                    break
            except Exception:
                continue

    for idx, (r, poly) in enumerate(polys):
        labels = labels_per_room.get(idx, [])
        if not labels:
            continue
        # Prefer labels containing digits
        chosen = None
        for lab in labels:
            if re.search(r"\d", lab.get("text", "")):
                chosen = lab
                break
        if chosen is None:
            # Prefer shortest non-trivial label
            labs_sorted = sorted([lab for lab in labels if len(lab.get("text", "").strip()) > 0], key=lambda x: len(x.get("text", "")))
            chosen = labs_sorted[0] if labs_sorted else labels[0]

        # Attach chosen label and store others as extra_labels in metadata
        r["label"] = chosen.get("text")
        extra = [lab.get("text") for lab in labels if lab is not chosen]
        if extra:
            meta = r.get("metadata") or {}
            meta.setdefault("extra_labels", [])
            meta["extra_labels"].extend(extra)
            r["metadata"] = meta


def _pick_label(labels: list[dict]) -> dict:
    """Pick the most informative label: prefer one with digits (room
    ID), else the shortest non-trivial string, else any label."""
    for lab in labels:
        if re.search(r"\d", lab.get("text", "") or ""):
            return lab
    nontrivial = [lab for lab in labels if (lab.get("text") or "").strip()]
    if nontrivial:
        return min(nontrivial, key=lambda x: len(x.get("text") or ""))
    return labels[0]


def _filter_by_labels(
    rooms: list[dict],
    texts: list[dict],
    *,
    boundary_buffer_m: float = _LABEL_BOUNDARY_BUFFER_M,
    nearest_fallback_m: float = _NEAREST_LABEL_FALLBACK_M,
    small_drop_area_m2: float = _SMALL_UNLABELED_DROP_AREA_M2,
    min_room_area_for_label_m2: float = _MIN_ROOM_AREA_FOR_LABEL_M2,
) -> tuple[list[dict], list[str], dict[str, int]]:
    """Attach labels to rooms and drop unlabeled-and-small polygons.

    The matching strategy works label-first (each label belongs to at
    most one polygon) and prefers small but real rooms over wall slivers
    by enforcing a minimum-area gate on label-claiming polygons.

    1. For each label, list polygons that either contain it or sit
       within `boundary_buffer_m` of the point.
    2. Among those, prefer ones with area >= `min_room_area_for_label_m2`
       (skips wall slivers); pick the smallest qualifier (innermost room).
    3. If none qualify but some are present below the area gate, attach
       to the largest of those (last resort).
    4. Polygons that ended up without any label are kept only if their
       area >= `small_drop_area_m2`. Unattached labels then attach to the
       nearest still-unlabeled polygon within `nearest_fallback_m` of
       the centroid (rescues leader-line / external labels).

    Returns `(kept_rooms, warnings, stats)`.
    """
    warnings: list[str] = []
    stats = {
        "dropped_unlabeled_small": 0,
        "kept_with_external_label": 0,
        "kept_unlabeled_large": 0,
        "rooms_with_contained_label": 0,
    }
    if not rooms:
        return [], warnings, stats

    polys: list[Optional[Polygon]] = []
    areas: list[float] = []
    for r in rooms:
        try:
            p = Polygon(r["polygon"])
            if not p.is_valid:
                p = None
        except Exception:
            p = None
        polys.append(p)
        areas.append(p.area if p is not None else 0.0)

    valid_indexed = [(i, p) for i, p in enumerate(polys) if p is not None]
    if not valid_indexed:
        return [], ["No valid polygons remain for label attachment."], stats

    try:
        from shapely.strtree import STRtree
        tree = STRtree([p for _, p in valid_indexed])
        idx_by_id = {id(p): i for i, p in valid_indexed}
        use_tree = True
    except Exception:
        tree = None
        idx_by_id = {}
        use_tree = False

    labels_per_room: dict[int, list[dict]] = {i: [] for i, _ in valid_indexed}
    unattached_labels: list[dict] = []

    for t in texts:
        try:
            pt = Point(float(t["x"]), float(t["y"]))
        except Exception:
            continue

        # Find candidates: polygons that contain the point or are near it.
        candidates: list[tuple[int, Polygon, float, bool]] = []  # (idx, poly, distance, strictly_contains)
        if use_tree:
            try:
                hits = tree.query(pt.buffer(boundary_buffer_m))
                cand_iter: list[tuple[int, Polygon]] = []
                for cand in hits:
                    try:
                        ci = int(cand)
                        if 0 <= ci < len(valid_indexed):
                            cand_iter.append(valid_indexed[ci])
                            continue
                    except (TypeError, ValueError):
                        pass
                    i = idx_by_id.get(id(cand))
                    if i is not None:
                        cand_iter.append((i, cand))
            except Exception:
                cand_iter = list(valid_indexed)
        else:
            cand_iter = list(valid_indexed)

        for i, poly in cand_iter:
            try:
                contains = poly.contains(pt)
                d = 0.0 if contains else poly.distance(pt)
            except Exception:
                continue
            if contains or d <= boundary_buffer_m:
                candidates.append((i, poly, d, contains))

        if not candidates:
            unattached_labels.append(t)
            continue

        # Prefer strictly-contained candidates over near-boundary ones.
        contained = [c for c in candidates if c[3]]
        pool = contained or candidates

        # Within the pool, prefer polygons whose area clears the
        # min_room_area gate. Among qualifiers, pick the smallest
        # (innermost room). If none clear the gate, fall back to the
        # largest sub-gate polygon (least bad).
        qualifying = [c for c in pool if areas[c[0]] >= min_room_area_for_label_m2]
        if qualifying:
            chosen_idx = min(qualifying, key=lambda c: areas[c[0]])[0]
        else:
            chosen_idx = max(pool, key=lambda c: areas[c[0]])[0]

        labels_per_room[chosen_idx].append(t)

    for i in labels_per_room:
        if labels_per_room[i]:
            stats["rooms_with_contained_label"] += 1

    # Pass 2: nearest-external fallback for still-unlabeled rooms.
    for i, poly in valid_indexed:
        if labels_per_room[i]:
            continue
        if areas[i] < min_room_area_for_label_m2:
            continue  # don't even try to attach external labels to slivers
        cx, cy = poly.centroid.x, poly.centroid.y
        best_d = nearest_fallback_m
        best_t: Optional[dict] = None
        for t in unattached_labels:
            try:
                d = math.hypot(float(t["x"]) - cx, float(t["y"]) - cy)
            except Exception:
                continue
            if d < best_d:
                best_d = d
                best_t = t
        if best_t is not None:
            labels_per_room[i].append({**best_t, "_source": "nearest_external"})
            stats["kept_with_external_label"] += 1

    kept: list[dict] = []
    stats["dropped_unlabeled_wall_like"] = 0
    for i, p in valid_indexed:
        labels = labels_per_room.get(i) or []
        rec = dict(rooms[i])
        meta = dict(rec.get("metadata") or {})
        if labels:
            chosen = _pick_label(labels)
            rec["label"] = chosen.get("text")
            meta["label_source"] = chosen.get("_source", "contained")
            extras = [lab.get("text") for lab in labels if lab is not chosen]
            if extras:
                meta["extra_labels"] = extras
        else:
            if areas[i] < small_drop_area_m2:
                stats["dropped_unlabeled_small"] += 1
                continue
            # Wall-like: high bbox aspect ratio. Drop unlabeled polygons
            # where the bbox is more than 6x longer than wide (typical for
            # corridor walls drawn as long thin rectangles).
            try:
                minx, miny, maxx, maxy = p.bounds
                w = maxx - minx
                h = maxy - miny
                long_dim = max(w, h)
                short_dim = max(min(w, h), 1e-6)
                aspect = long_dim / short_dim
            except Exception:
                aspect = 1.0
            if aspect > 6.0:
                stats["dropped_unlabeled_wall_like"] += 1
                continue
            stats["kept_unlabeled_large"] += 1
            meta["label_source"] = "unlabeled"
        rec["metadata"] = meta
        kept.append(rec)

    if stats["dropped_unlabeled_small"]:
        warnings.append(
            f"Dropped {stats['dropped_unlabeled_small']} unlabeled polygons smaller than "
            f"{small_drop_area_m2:.1f} m^2 (likely furniture/fixtures)."
        )
    if stats["kept_with_external_label"]:
        warnings.append(
            f"{stats['kept_with_external_label']} rooms attached labels via "
            f"nearest-external fallback (within {nearest_fallback_m:.1f} m of centroid)."
        )
    if stats["kept_unlabeled_large"]:
        warnings.append(
            f"{stats['kept_unlabeled_large']} unlabeled polygons kept on the strength of "
            f"area >= {small_drop_area_m2:.1f} m^2."
        )
    return kept, warnings, stats


def _drop_outer_envelopes(rooms: list[dict]) -> tuple[list[dict], int]:
    """Drop polygons that fully contain three or more other accepted
    polygons — these are whole-floor outlines, not rooms.
    Returns `(kept, dropped_count)`.
    """
    if len(rooms) < 4:
        return rooms, 0
    shapes: list[Optional[Polygon]] = []
    for r in rooms:
        try:
            shapes.append(Polygon(r["polygon"]))
        except Exception:
            shapes.append(None)
    drop: set[int] = set()
    for i, big in enumerate(shapes):
        if big is None or i in drop:
            continue
        contained = 0
        for j, s in enumerate(shapes):
            if i == j or s is None or j in drop:
                continue
            try:
                if big.contains(s):
                    contained += 1
                    if contained >= 3:
                        break
            except Exception:
                continue
        if contained >= 3:
            drop.add(i)
    if not drop:
        return rooms, 0
    return [r for i, r in enumerate(rooms) if i not in drop], len(drop)


def _merge_id_type_pairs(
    spaces: list[dict],
    *,
    pairing_radius_m: float = 5.0,
) -> tuple[list[dict], dict[str, str], int]:
    """Merge pairs of spaces that represent one physical room.

    In CAD floor plans the room ID label (e.g. "2.0.041") and the room
    type label (e.g. "Audio Workshop") are often placed several meters
    apart inside the same room. After polygonize + label attachment,
    each label may end up on a different polygon. This step identifies
    pairs whose centroids are within `pairing_radius_m` and where one
    space carries a numeric `short_name` while the other does not, then
    folds the type-bearing space into the ID-bearing one.

    Returns `(merged_spaces, id_remap, merge_count)` where `id_remap`
    maps dropped-space-ids to surviving-space-ids so the caller can
    rewrite connections.
    """
    if not spaces:
        return spaces, {}, 0

    id_spaces: list[dict] = []   # short_name has digits
    type_spaces: list[dict] = []  # purely alphabetic name
    other: list[dict] = []
    for s in spaces:
        sn = s.get("short_name")
        name = s.get("display_name") or ""
        if sn and any(c.isdigit() for c in sn):
            id_spaces.append(s)
        elif name and not name.startswith("Room ") and any(c.isalpha() for c in name) and not any(c.isdigit() for c in name):
            type_spaces.append(s)
        else:
            other.append(s)

    if not id_spaces or not type_spaces:
        return spaces, {}, 0

    id_remap: dict[str, str] = {}
    consumed: set[int] = set()
    surviving = list(id_spaces)

    for ti, t_space in enumerate(type_spaces):
        tx = t_space.get("centroid_x")
        ty = t_space.get("centroid_y")
        if tx is None or ty is None:
            continue
        best_id_idx: Optional[int] = None
        best_d = pairing_radius_m
        for ii, i_space in enumerate(surviving):
            ix = i_space.get("centroid_x")
            iy = i_space.get("centroid_y")
            if ix is None or iy is None:
                continue
            d = math.hypot(tx - ix, ty - iy)
            if d < best_d:
                best_d = d
                best_id_idx = ii
        if best_id_idx is None:
            continue

        target = surviving[best_id_idx]
        consumed.add(ti)
        id_remap[t_space["id"]] = target["id"]

        # Promote the type-name's classification if it's more specific.
        if target.get("space_type") == "ROOM_GENERIC" and t_space.get("space_type") != "ROOM_GENERIC":
            target["space_type"] = t_space["space_type"]
            target["is_navigable"] = t_space.get("is_navigable", target.get("is_navigable", False))

        # Append the type-name and any extras to the survivor's metadata.
        meta = dict(target.get("metadata") or {})
        extras = list(meta.get("extra_labels") or [])
        if t_space.get("display_name"):
            extras.append(t_space["display_name"])
        for e in (t_space.get("metadata") or {}).get("extra_labels") or []:
            extras.append(e)
        # dedupe while preserving order
        seen = set()
        deduped: list[str] = []
        for e in extras:
            if e and e not in seen:
                seen.add(e)
                deduped.append(e)
        if deduped:
            meta["extra_labels"] = deduped
        meta["merged_from"] = (meta.get("merged_from") or []) + [t_space["id"]]
        target["metadata"] = meta

        # Use the larger polygon if the type-space's polygon is bigger.
        try:
            if t_space.get("area_m2") and target.get("area_m2"):
                if t_space["area_m2"] > target["area_m2"]:
                    target["polygon"] = t_space["polygon"]
                    target["centroid_x"] = t_space.get("centroid_x")
                    target["centroid_y"] = t_space.get("centroid_y")
                    target["area_m2"] = t_space["area_m2"]
                    target["width_m"] = t_space.get("width_m")
                    target["length_m"] = t_space.get("length_m")
        except Exception:
            pass

    surviving_type = [s for ti, s in enumerate(type_spaces) if ti not in consumed]
    merged = [*surviving, *surviving_type, *other]
    return merged, id_remap, len(consumed)


def _collect_diagnostics_from_doc(doc: Drawing) -> dict:
    msp = doc.modelspace()
    entity_counts = Counter()
    layer_counts = Counter()
    type_layer_counts = Counter()
    closed_poly_count = 0
    text_count = 0
    insert_count = 0
    hatch_count = 0
    line_arc_count = 0
    for e in msp:
        try:
            t = e.dxftype()
        except Exception:
            continue
        entity_counts[t] += 1
        layer = getattr(e.dxf, "layer", "") or ""
        layer_counts[layer] += 1
        type_layer_counts[(t, layer)] += 1
        if t in ("LWPOLYLINE", "POLYLINE"):
            if (getattr(e, "closed", False) or getattr(e, "is_closed", False)):
                closed_poly_count += 1
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            text_count += 1
        if t == "INSERT":
            insert_count += 1
        if t == "HATCH":
            hatch_count += 1
        if t in ("LINE", "ARC", "CIRCLE"):
            line_arc_count += 1

    return {
        "entity_counts": dict(entity_counts),
        "layer_counts": dict(layer_counts),
        "type_layer_counts": {f"{k[0]}|{k[1]}": v for k, v in type_layer_counts.items()},
        "closed_poly_count": closed_poly_count,
        "text_count": text_count,
        "insert_count": insert_count,
        "hatch_count": hatch_count,
        "line_arc_count": line_arc_count,
    }


def _collect_diagnostics_from_parsed(parsed: dict) -> dict:
    entities = parsed.get("entities") or []
    entity_counts = Counter()
    layer_counts = Counter()
    type_layer_counts = Counter()
    closed_poly_count = 0
    text_count = 0
    insert_count = 0
    hatch_count = 0
    line_arc_count = 0
    for e in entities:
        t = (e.get("type") or "").upper()
        layer = (e.get("layer") or "")
        entity_counts[t] += 1
        layer_counts[layer] += 1
        type_layer_counts[(t, layer)] += 1
        if t in ("LWPOLYLINE", "POLYLINE"):
            verts = e.get("vertices") or []
            flag = e.get("flag") or e.get("flags") or 0
            closed = False
            try:
                if int(flag) & 1:
                    closed = True
            except Exception:
                closed = False
            if not closed and verts and verts[0] == verts[-1]:
                closed = True
            if closed:
                closed_poly_count += 1
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            text_count += 1
        if t == "INSERT":
            insert_count += 1
        if t == "HATCH":
            hatch_count += 1
        if t in ("LINE", "ARC", "CIRCLE"):
            line_arc_count += 1

    return {
        "entity_counts": dict(entity_counts),
        "layer_counts": dict(layer_counts),
        "type_layer_counts": {f"{k[0]}|{k[1]}": v for k, v in type_layer_counts.items()},
        "closed_poly_count": closed_poly_count,
        "text_count": text_count,
        "insert_count": insert_count,
        "hatch_count": hatch_count,
        "line_arc_count": line_arc_count,
    }


def _classify_space(
    layer_name: Optional[str],
    label: Optional[str],
    overrides: Optional[dict[str, str]] = None,
) -> tuple[str, bool, float]:
    """Classify a space label-first, then layer-fallback.

    Priority: explicit override on layer name -> label keyword match ->
    layer keyword match -> ROOM_GENERIC. Returns
    `(space_type, is_navigable, confidence)`.
    """
    if overrides and layer_name:
        for k, v in overrides.items():
            if k and k.upper() == layer_name.upper():
                return v, (v in _NAVIGABLE_TYPES), 0.95

    label_type, label_conf = _classify_label(label)
    if label_type != "ROOM_GENERIC":
        return label_type, (label_type in _NAVIGABLE_TYPES), label_conf

    if layer_name:
        layer_type, _ = _classify_label(layer_name)
        if layer_type != "ROOM_GENERIC":
            return layer_type, (layer_type in _NAVIGABLE_TYPES), 0.6

    return "ROOM_GENERIC", False, 0.4


def _extract_hatch_polygons(doc: Drawing) -> tuple[list[dict], list[str]]:
    """Attempt to extract polygons from HATCH entities. This is
    best-effort and will produce warnings when hatch paths/edges are
    not supported by the simple extractor.
    Returns (list_of_room_dicts, warnings)
    """
    out: list[dict] = []
    warnings: list[str] = []
    try:
        msp = doc.modelspace()
    except Exception:
        return out, warnings

    for h in msp.query("HATCH"):
        try:
            loops: list[list[tuple[float, float]]] = []
            # ezdxf exposes paths on Hatch objects; each path has edges
            for path in getattr(h, "paths", []):
                coords: list[tuple[float, float]] = []
                for edge in getattr(path, "edges", []):
                    et = edge.dxftype() if hasattr(edge, "dxftype") else ""
                    if et == "LINE" or et == "LineEdge":
                        try:
                            a = getattr(edge, "start", None) or getattr(edge, "start_point", None)
                            b = getattr(edge, "end", None) or getattr(edge, "end_point", None)
                            if a and b:
                                coords.append((float(a[0]), float(a[1])))
                        except Exception:
                            continue
                    elif et in ("ARC", "ArcEdge"):
                        try:
                            c = getattr(edge, "center", None)
                            r = getattr(edge, "radius", None)
                            st = getattr(edge, "start_angle", None) or getattr(edge, "start", 0)
                            ed = getattr(edge, "end_angle", None) or getattr(edge, "end", 0)
                            if c and r:
                                pts = _approximate_arc_points(float(c[0]), float(c[1]), float(r), float(st), float(ed), steps=8)
                                coords.extend(pts)
                        except Exception:
                            warnings.append(f"HATCH: unsupported arc edge in hatch on layer {getattr(h.dxf, 'layer', '')}")
                            continue
                    else:
                        # Unsupported edge: try to see if it exposes points
                        pts = getattr(edge, "points", None)
                        if pts:
                            for p in pts:
                                try:
                                    coords.append((float(p[0]), float(p[1])))
                                except Exception:
                                    continue
                if coords:
                    # Ensure closed
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    loops.append(coords)
            for poly in loops:
                out.append({"layer": getattr(h.dxf, "layer", "") or "", "polygon": poly, "source": "hatch"})
        except Exception as exc:
            warnings.append(f"HATCH extraction failed: {exc}")
            continue
    return out, warnings


def _arc_is_door_candidate(radius_m: float, sweep_deg: float) -> bool:
    """A door swing has a characteristic geometry: leaf-width radius
    (0.4-1.5 m) and a 45-130 deg arc sweep. Round tables, decorative
    fillets and very long curves are filtered out."""
    if not (_DOOR_ARC_RADIUS_RANGE_M[0] <= radius_m <= _DOOR_ARC_RADIUS_RANGE_M[1]):
        return False
    return _DOOR_ARC_SWEEP_DEG[0] <= sweep_deg <= _DOOR_ARC_SWEEP_DEG[1]


def _collect_door_candidates(
    doc: Drawing,
    scale_m: float,
) -> tuple[list[tuple[tuple[float, float], str]], int]:
    """Collect plausible door points already converted to meters.

    Returns `(candidates, arc_count)` where `candidates` is a list of
    `((x_m, y_m), source)` and `source` is "arc_geometry" or
    "door_layer". `arc_count` is the number of ARC entities that passed
    the door-shape filter (useful in diagnostics).
    """
    out: list[tuple[tuple[float, float], str]] = []
    arc_hits = 0
    try:
        msp = doc.modelspace()
    except Exception:
        return out, 0

    for e in msp:
        try:
            t = e.dxftype()
        except Exception:
            continue

        layer = (getattr(e.dxf, "layer", "") or "").upper()
        layer_door = (
            "DOOR" in layer
            or "DØR" in layer
            or layer in {"DR", "A-DOOR"}
        )

        if t == "ARC":
            try:
                radius_m = float(e.dxf.radius) * scale_m
                start = float(e.dxf.start_angle)
                end = float(e.dxf.end_angle)
                sweep = abs(end - start)
                if sweep > 360:
                    sweep = sweep % 360
                if sweep > 180:
                    sweep = 360 - sweep
                if _arc_is_door_candidate(radius_m, sweep):
                    c = e.dxf.center
                    cx = float(c[0]) * scale_m
                    cy = float(c[1]) * scale_m
                    mid_rad = math.radians((start + end) / 2.0)
                    mx = cx + radius_m * math.cos(mid_rad) * 0.5
                    my = cy + radius_m * math.sin(mid_rad) * 0.5
                    out.append(((mx, my), "arc_geometry"))
                    arc_hits += 1
            except Exception:
                continue
        elif layer_door:
            try:
                if t == "INSERT":
                    pt = getattr(e.dxf, "insert", None)
                    if pt:
                        out.append((
                            (float(pt[0]) * scale_m, float(pt[1]) * scale_m),
                            "door_layer",
                        ))
                elif t == "LINE":
                    s = e.dxf.start
                    ed = e.dxf.end
                    mx = (float(s[0]) + float(ed[0])) / 2.0 * scale_m
                    my = (float(s[1]) + float(ed[1])) / 2.0 * scale_m
                    out.append(((mx, my), "door_layer"))
                elif t == "CIRCLE":
                    c = e.dxf.center
                    out.append((
                        (float(c[0]) * scale_m, float(c[1]) * scale_m),
                        "door_layer",
                    ))
            except Exception:
                continue
    return out, arc_hits


def _infer_connections(
    spaces: list[dict],
    doc: Optional[Drawing],
    scale_m: float,
) -> tuple[list[dict], list[str], int]:
    """Arc + door-layer hybrid door connection inference.

    A door is inferred when the candidate point is within
    `_DOOR_CONNECTION_THRESHOLD_M` of two distinct rooms. Geometry has
    already been scaled to meters, so the threshold is in meters.

    Returns `(connections, warnings, arc_candidate_count)`.
    """
    warnings: list[str] = []
    connections: list[dict] = []
    if not doc:
        return connections, warnings, 0

    candidates, arc_hits = _collect_door_candidates(doc, scale_m)
    if not candidates:
        warnings.append("No door candidates (no qualifying ARCs and no DOOR layers).")
        return connections, warnings, arc_hits

    polys: list[tuple[str, Polygon]] = []
    for s in spaces:
        try:
            p = Polygon(s["polygon"]) if s.get("polygon") else None
            if p is not None and p.is_valid:
                polys.append((s["id"], p))
        except Exception:
            continue
    if len(polys) < 2:
        return connections, warnings, arc_hits

    use_tree = False
    tree = None
    poly_by_id: dict[int, tuple[str, Polygon]] = {}
    try:
        from shapely.strtree import STRtree
        tree = STRtree([p for _, p in polys])
        poly_by_id = {id(p): (sid, p) for sid, p in polys}
        use_tree = True
    except Exception:
        use_tree = False

    seen: set[tuple[str, str]] = set()
    for (x, y), source in candidates:
        pt = Point(x, y)
        if use_tree:
            try:
                hits = tree.query(pt.buffer(1.0))
                near: list[tuple[str, Polygon]] = []
                for cand in hits:
                    try:
                        ci = int(cand)
                        if 0 <= ci < len(polys):
                            near.append(polys[ci])
                            continue
                    except (TypeError, ValueError):
                        pass
                    rec = poly_by_id.get(id(cand))
                    if rec is not None:
                        near.append(rec)
                if not near:
                    near = polys
            except Exception:
                near = polys
        else:
            near = polys

        dists: list[tuple[str, float]] = []
        for sid, poly in near:
            try:
                d = poly.distance(pt)
            except Exception:
                continue
            if d <= _DOOR_CONNECTION_THRESHOLD_M:
                dists.append((sid, d))
        if len(dists) < 2:
            continue
        dists.sort(key=lambda kv: kv[1])
        a, b = dists[0][0], dists[1][0]
        if a == b:
            continue
        key = (a, b) if a <= b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        connections.append({
            "from_space_id": a,
            "to_space_id": b,
            "connection_type": "DOOR",
            "door_type": "STANDARD",
            "is_accessible": True,
            "requires_access_level": None,
            "transition_time_s": 5.0,
            "weight_override": None,
            "door_cx": x,
            "door_cy": y,
            "metadata": {"source": source},
        })

    if connections:
        warnings.append(
            f"Inferred {len(connections)} door connection(s) from "
            f"{arc_hits} arc candidate(s) + door layers."
        )
    return connections, warnings, arc_hits


def _convert_dwg_with_oda(dwg_bytes: bytes) -> bytes:
    """ODA File Converter path. Highest fidelity but needs the proprietary
    binary (free download from the Open Design Alliance, not redistributable)."""
    with tempfile.TemporaryDirectory() as in_dir, tempfile.TemporaryDirectory() as out_dir:
        in_path = Path(in_dir) / "input.dwg"
        in_path.write_bytes(dwg_bytes)
        proc = subprocess.run(
            ["ODAFileConverter", in_dir, out_dir, "ACAD2018", "DXF", "0", "1"],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ODAFileConverter failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='ignore')[:400]}"
            )
        out_path = Path(out_dir) / "input.dxf"
        if not out_path.exists():
            raise RuntimeError("ODAFileConverter ran but did not produce a DXF output")
        return out_path.read_bytes()


def _convert_dwg_with_libredwg(dwg_bytes: bytes) -> bytes:
    """LibreDWG (`dwgread`) path. GPL, freely redistributable, available
    in Debian as the `libredwg` package. Lower fidelity than ODA on
    quirky AutoCAD revisions but sufficient for the floor-plan vocabulary
    (LWPOLYLINE, POLYLINE, TEXT, MTEXT, INSERT) that we actually consume."""
    with tempfile.TemporaryDirectory() as work_dir:
        in_path = Path(work_dir) / "input.dwg"
        out_path = Path(work_dir) / "input.dxf"
        in_path.write_bytes(dwg_bytes)
        # `dwgread -O DXF input.dwg -o output.dxf` produces an ASCII DXF.
        proc = subprocess.run(
            ["dwgread", "-O", "DXF", "-o", str(out_path), str(in_path)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"dwgread failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='ignore')[:400]}"
            )
        if not out_path.exists():
            raise RuntimeError("dwgread ran but did not produce a DXF output")
        return out_path.read_bytes()


def _convert_dwg_to_dxf(dwg_bytes: bytes) -> bytes:
    """Best-effort DWG → DXF transcode. Tries the converters in order of
    fidelity (ODA → LibreDWG); raises FileNotFoundError with a clear
    actionable message when neither is installed."""
    if shutil.which("ODAFileConverter"):
        return _convert_dwg_with_oda(dwg_bytes)
    if shutil.which("dwgread"):
        return _convert_dwg_with_libredwg(dwg_bytes)
    raise FileNotFoundError(
        "No DWG converter is installed on the backend. Three ways to fix this:\n"
    )


def _sanitize_libredwg_dxf(dxf_bytes: bytes) -> tuple[bytes, list[str]]:
    """Strip malformed code/value pairs from a LibreDWG-produced DXF."""
    text = dxf_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    out: list[str] = []
    warnings: list[str] = []
    dropped = 0
    i = 0
    n = len(lines)
    while i < n - 1:
        code_line = lines[i]
        stripped = code_line.strip()
        try:
            code = int(stripped)
            if code < 0 or code > 1071:
                raise ValueError("group code out of range")
        except ValueError:
            dropped += 1
            if dropped <= 5:
                value_peek = lines[i + 1].strip() if i + 1 < n else ""
                warnings.append(
                    f"DWG→DXF sanitizer: dropped malformed line {i + 1}: "
                    f"{stripped!r} (next line: {value_peek!r})"
                )
            i += 1
            continue
        out.append(code_line)
        out.append(lines[i + 1])
        i += 2
    if i < n:
        out.append(lines[i])
    if dropped > 5:
        warnings.append(
            f"DWG→DXF sanitizer: dropped {dropped - 5} additional malformed lines"
        )
    return ("\n".join(out) + "\n").encode("utf-8"), warnings


def _read_dxf_robust(dxf_bytes: bytes) -> tuple[Drawing, list[str]]:
    """Read a DXF (ASCII or binary) using ezdxf's recovery path."""
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(dxf_bytes)
        tmp_path = tmp.name
    try:
        try:
            doc, auditor = ezdxf_recover.readfile(tmp_path)
        except ezdxf.DXFStructureError as exc:
            raise ValueError(
                f"DXF file structure is too damaged to recover: {exc}"
            ) from exc
        warnings: list[str] = []
        for err in getattr(auditor, "errors", [])[:50]:
            warnings.append(f"DXF auditor: {err}")
        for fix in getattr(auditor, "fixes", [])[:50]:
            warnings.append(f"DXF auditor (auto-fixed): {fix}")
        return doc, warnings
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


def _validate_origin(lat: Optional[float], lng: Optional[float]) -> None:
    if lat is None and lng is None:
        return
    if lat is None or lng is None:
        raise ValueError("origin_lat and origin_lng must be supplied together (or both omitted)")
    if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1]):
        raise ValueError(f"origin_lat {lat} out of range {_LAT_RANGE}")
    if not (_LNG_RANGE[0] <= lng <= _LNG_RANGE[1]):
        raise ValueError(f"origin_lng {lng} out of range {_LNG_RANGE}")


class DxfImportService:
    """Translates a DXF (or a DWG that we first transcode) into a
    `MapImportSchema`-compatible dict."""

    def parse(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        campus_id: str,
        campus_name: str,
        building_id: str,
        building_name: str,
        floor_id: str,
        floor_index: int,
        floor_display_name: str,
        organization_id: Optional[str] = None,
        organization_name: Optional[str] = None,
        origin_lat: Optional[float] = None,
        origin_lng: Optional[float] = None,
        origin_bearing: float = 0.0,
        layer_mapping: Optional[dict[str, str]] = None,
        enable_polygonize: bool = False,
    ) -> dict[str, Any]:
        _validate_origin(origin_lat, origin_lng)

        if len(file_bytes) > _MAX_DXF_BYTES:
            raise ValueError(
                f"File size {len(file_bytes)} exceeds maximum of {_MAX_DXF_BYTES} bytes. "
            )

        import time
        import logging
        log = logging.getLogger("dxf_import")
        t0 = time.monotonic()
        def _stage(label: str, started: float, **extra) -> None:
            log.info("[dxf-import] %s took %.2fs %s", label, time.monotonic() - started, extra or "")

        ext = Path(filename or "").suffix.lower()
        from_dwg = False
        if ext == ".dxf":
            dxf_bytes = file_bytes
        elif ext == ".dwg":
            t = time.monotonic()
            dxf_bytes = _convert_dwg_to_dxf(file_bytes)
            from_dwg = True
            _stage("dwg→dxf transcode", t, output_bytes=len(dxf_bytes))
        else:
            raise ValueError(
                f"Unsupported file extension '{ext}'. Upload a .dxf or a .dwg."
            )

        sanitize_warnings: list[str] = []
        t = time.monotonic()
        try:
            doc, dxf_warnings = _read_dxf_robust(dxf_bytes)
        except ValueError as exc:
            if from_dwg and "Invalid group code" in str(exc):
                sanitized_bytes, sanitize_warnings = _sanitize_libredwg_dxf(dxf_bytes)
                try:
                    doc, dxf_warnings = _read_dxf_robust(sanitized_bytes)
                except ValueError as exc2:
                    raise ValueError(
                        "The DWG was transcoded to DXF on the server, but the "
                        "resulting DXF is structurally malformed and the "
                        "auto-sanitizer pass could not salvage it either. "
                        f"Underlying error: {exc2}\n\n"
                        "Two ways to get this DWG imported:\n"
                        "  1) Convert the .dwg to .dxf locally with any CAD tool "
                        "(LibreCAD, DraftSight, BricsCAD, AutoCAD, or the standalone "
                        "ODA File Converter) and re-upload the .dxf.\n"
                        "  2) Install ODAFileConverter into the backend image for "
                        "higher-fidelity DWG support:\n"
                        "       docker compose build --build-arg INSTALL_ODA=1 \\\n"
                        "         --build-arg ODA_DEB_URL=https://your-mirror/...deb backend"
                    ) from exc2
            else:
                raise
        if sanitize_warnings:
            dxf_warnings = [*sanitize_warnings, *dxf_warnings]
        _stage("ezdxf parse", t)

        # Gather diagnostics
        t = time.monotonic()
        diagnostics = _collect_diagnostics_from_doc(doc)
        _stage("diagnostics", t)

        # Resolve units up front so the label-seeded polygonize knows how
        # large its search bbox should be in raw DXF units. For
        # $INSUNITS=0 (unitless) we get scale=1.0 with a warning; the
        # post-extraction bbox heuristic refines this if needed.
        unit_scale, unit_warnings = _resolve_unit_scale(doc, polygon_bbox_span=None)
        unit_known_from_header = not any(
            "no polygon bbox" in w or "$INSUNITS=0" in w for w in unit_warnings
        )

        t = time.monotonic()
        raw_rooms = _walk_polygons(doc.modelspace())
        _stage("walk_polygons", t, count=len(raw_rooms))

        t = time.monotonic()
        hatch_rooms, hatch_warnings = _extract_hatch_polygons(doc)
        _stage("hatch", t, count=len(hatch_rooms))
        if hatch_rooms:
            raw_rooms = [*raw_rooms, *hatch_rooms]

        t = time.monotonic()
        texts = _extract_texts(doc)
        if len(texts) > _MAX_TEXT_ENTITIES:
            text_truncate_warning = (
                f"Found {len(texts)} text entities; truncated to {_MAX_TEXT_ENTITIES} for label attachment."
            )
            texts = texts[:_MAX_TEXT_ENTITIES]
        else:
            text_truncate_warning = None
        _stage("extract_texts", t, count=len(texts))

        # Global polygonize from raw line/arc segments. Default ON because
        # CAD drawings often draw rooms as wall lines, not closed polygons.
        # The snap tolerance is the key knob: too tight (e.g. 0.15mm on a
        # mm-based file) and corner gaps prevent loop closure; ~20mm of
        # real-world distance reliably closes typical CAD wall corners
        # without merging unrelated geometry.
        polygonize_skipped_reason: Optional[str] = None
        polygonize_warnings: list[str] = []
        if enable_polygonize:
            snap_tol_raw = max(0.020 / unit_scale, _POLYGONIZE_SNAP_TOLERANCE)
            t = time.monotonic()
            try:
                poly_rooms, polygonize_warnings = _polygonize_from_doc(
                    doc, snap_tolerance_raw=snap_tol_raw,
                )
            except Exception as exc:
                poly_rooms = []
                polygonize_skipped_reason = f"polygonization failed: {exc}"
            _stage("polygonize", t, count=len(poly_rooms), snap_tol=snap_tol_raw)
            if poly_rooms:
                raw_rooms = [*raw_rooms, *poly_rooms]
        else:
            polygonize_skipped_reason = (
                "polygonize disabled by request (enable_polygonize=false); "
                "rooms come only from closed polylines and HATCHes."
            )

        t = time.monotonic()
        rooms, geom_warnings = _validate_and_dedupe(raw_rooms)
        _stage("validate_and_dedupe", t, kept=len(rooms), input=len(raw_rooms))
        if polygonize_skipped_reason:
            geom_warnings.append(polygonize_skipped_reason)
        if text_truncate_warning:
            geom_warnings.append(text_truncate_warning)
        if polygonize_warnings:
            geom_warnings.extend(polygonize_warnings)
        if len(rooms) > _MAX_FINAL_ROOMS:
            raise ValueError(
                f"Too many rooms detected ({len(rooms)}); the parser caps at {_MAX_FINAL_ROOMS} to avoid out-of-memory hangs. "
            )

        # If $INSUNITS was 0, refine the unit scale now that we have
        # polygons in raw units.
        if not unit_known_from_header and rooms:
            bbox_span = 0.0
            for r in rooms:
                poly = r.get("polygon") or []
                if not poly:
                    continue
                xs_r = [p[0] for p in poly]
                ys_r = [p[1] for p in poly]
                if xs_r and ys_r:
                    bbox_span = max(bbox_span, max(xs_r) - min(xs_r), max(ys_r) - min(ys_r))
            unit_scale, refined_warnings = _resolve_unit_scale(doc, bbox_span)
            unit_warnings = [*unit_warnings, *refined_warnings]

        _scale_rooms(rooms, unit_scale)
        rooms = [
            r for r in rooms
            if _MIN_ROOM_AREA_M2 <= _polygon_area_m2(r["polygon"]) <= _MAX_ROOM_AREA_M2
        ]
        _scale_texts(texts, unit_scale)

        t = time.monotonic()
        rooms, label_warnings, label_stats = _filter_by_labels(rooms, texts)
        rooms, envelopes_dropped = _drop_outer_envelopes(rooms)
        _stage("filter_by_labels", t,
               kept=len(rooms),
               envelopes_dropped=envelopes_dropped,
               dropped_unlabeled_small=label_stats.get("dropped_unlabeled_small", 0))

        spaces: list[dict] = []
        for i, r in enumerate(rooms, start=1):
            sid = f"{floor_id}_space_{i}"
            label = r.get("label") or f"Room {i}"
            polygon = r["polygon"]
            src = r.get("source") or "closed_polyline"
            try:
                shape = Polygon(polygon)
                cx, cy = float(shape.centroid.x), float(shape.centroid.y)
                area = float(shape.area)
                minx, miny, maxx, maxy = shape.bounds
                width = float(maxx - minx)
                length = float(maxy - miny)
            except Exception:
                cx = sum(p[0] for p in polygon) / len(polygon)
                cy = sum(p[1] for p in polygon) / len(polygon)
                try:
                    shape = Polygon(polygon)
                    area = float(shape.area)
                    minx, miny, maxx, maxy = shape.bounds
                    width = float(maxx - minx)
                    length = float(maxy - miny)
                except Exception:
                    area = 0.0
                    width = 0.0
                    length = 0.0

            tokens = re.findall(r"\b[\w-]+\b", label)
            short_name: Optional[str] = None
            for tok in tokens:
                if any(ch.isdigit() for ch in tok):
                    short_name = tok
                    break

            tags: list[str] = []
            layer = (r.get("layer") or "").strip()
            if layer:
                tags.append(layer.lower())
            for tok in tokens:
                tl = tok.lower()
                if len(tl) > 1 and tl not in tags:
                    tags.append(tl)
            tags.append(src)

            # Classify against the primary label first; if it's generic
            # (e.g. a numeric ID like "0.054"), try the extras labels —
            # the type-name often lives there ("Lager", "Teknik", etc.).
            space_type, is_nav, class_conf = _classify_space(layer, label, overrides=layer_mapping)
            if space_type == "ROOM_GENERIC":
                meta_extras = (r.get("metadata") or {}).get("extra_labels") or []
                for extra in meta_extras:
                    e_type, e_nav, e_conf = _classify_space(layer, extra, overrides=layer_mapping)
                    if e_type != "ROOM_GENERIC":
                        space_type, is_nav, class_conf = e_type, e_nav, e_conf
                        break
            base_conf = 0.95 if src == "closed_polyline" else 0.85 if src == "hatch" else 0.6
            confidence = min(1.0, base_conf * 0.7 + class_conf * 0.3)

            meta = dict(r.get("metadata") or {})
            meta.setdefault("source_layer", layer or None)
            meta.setdefault("source_geometry_type", src)
            meta.setdefault("confidence", round(confidence, 2))
            meta.setdefault("warnings", [])

            spaces.append({
                "id": sid,
                "display_name": label,
                "short_name": short_name,
                "space_type": space_type,
                "polygon": [[float(x), float(y)] for x, y in polygon],
                "centroid_x": cx,
                "centroid_y": cy,
                "area_m2": round(area, 3),
                "width_m": round(width, 3),
                "length_m": round(length, 3),
                "is_navigable": bool(is_nav),
                "is_accessible": True,
                "is_outdoor": False,
                "capacity": None,
                "tags": tags,
                "metadata": meta,
                "subspaces": [],
            })

        # Merge spaces that look like the same physical room split into
        # ID-bearing and type-bearing halves (CAD plans often place those
        # labels several meters apart).
        t = time.monotonic()
        spaces, id_remap, merged_count = _merge_id_type_pairs(spaces)
        _stage("merge_id_type_pairs", t, merged=merged_count, kept=len(spaces))

        xs: list[float] = []
        ys: list[float] = []
        for s in spaces:
            for x, y in s.get("polygon", []):
                xs.append(x)
                ys.append(y)
        if xs and ys:
            min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
            building_bounds = [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]
            floor_plan_bounds = building_bounds
        else:
            building_bounds = None
            floor_plan_bounds = None

        schema: dict[str, Any] = {
            "schema_version": "1.0",
            "campus": {
                "id": campus_id,
                "name": campus_name,
                "description": building_name or None,
                "organization_id": organization_id,
                "is_public": False,
                "buildings": [{
                    "id": building_id,
                    "name": building_name,
                    "short_name": (building_name.split()[0] if building_name else None),
                    "organization_id": organization_id,
                    "origin_lat": origin_lat,
                    "origin_lng": origin_lng,
                    "origin_bearing": origin_bearing,
                    "floor_count": 1,
                    "floors": [{
                        "id": floor_id,
                        "floor_index": floor_index,
                        "display_name": floor_display_name,
                        "floor_plan_bounds": floor_plan_bounds,
                        "spaces": spaces,
                    }],
                    "building_bounds": building_bounds,
                }],
                "outdoor_spaces": [],
                "connections": [],
            },
        }
        if organization_id and organization_name:
            schema["organization"] = {
                "id": organization_id,
                "name": organization_name,
                "entity_type": "OTHER",
            }

        t = time.monotonic()
        arc_candidates = 0
        if not spaces:
            connections, conn_warnings = [], []
        elif len(spaces) > _MAX_FINAL_ROOMS // 2:
            connections, conn_warnings = [], [
                f"Skipped door inference: {len(spaces)} spaces is past the safety threshold; "
                f"author connections in the editor instead."
            ]
        else:
            connections, conn_warnings, arc_candidates = _infer_connections(spaces, doc, unit_scale)
        _stage("infer_connections", t, connections=len(connections), arc_candidates=arc_candidates)

        # If any spaces were merged, rewrite connection endpoints that
        # still reference the absorbed (dropped) space ids. Defensive — the
        # arc-based inference uses the post-merge spaces list, so this
        # should normally be a no-op.
        if id_remap:
            for c in connections:
                if c.get("from_space_id") in id_remap:
                    c["from_space_id"] = id_remap[c["from_space_id"]]
                if c.get("to_space_id") in id_remap:
                    c["to_space_id"] = id_remap[c["to_space_id"]]
            connections = [c for c in connections if c["from_space_id"] != c["to_space_id"]]

        schema["campus"]["connections"] = connections

        diagnostics_out = diagnostics.copy() if isinstance(diagnostics, dict) else {}
        diagnostics_out["raw_room_count"] = len(raw_rooms) if isinstance(raw_rooms, list) else None
        diagnostics_out["cleaned_room_count"] = len(rooms)
        diagnostics_out["parsing_engine"] = "ezdxf"
        diagnostics_out["unit_scale_m"] = unit_scale
        diagnostics_out["polygons_dropped_unlabeled_small"] = label_stats.get("dropped_unlabeled_small", 0)
        diagnostics_out["polygons_kept_external_label"] = label_stats.get("kept_with_external_label", 0)
        diagnostics_out["polygons_kept_unlabeled_large"] = label_stats.get("kept_unlabeled_large", 0)
        diagnostics_out["polygons_dropped_envelope"] = envelopes_dropped
        diagnostics_out["door_arc_candidates"] = arc_candidates
        diagnostics_out["polygonize_skipped_reason"] = polygonize_skipped_reason
        diagnostics_out["id_type_pairs_merged"] = merged_count
        schema["_diagnostics"] = diagnostics_out

        schema["_warnings"] = [
            *dxf_warnings,
            *hatch_warnings,
            *geom_warnings,
            *unit_warnings,
            *label_warnings,
            *conn_warnings,
        ]
        if not rooms:
            schema["_warnings"].append(
                "No labeled rooms detected — review file or layer mapping. "
                "If the drawing has only raw linework, retry with enable_polygonize=true."
            )
        elif len(rooms) <= 1:
            schema["_warnings"].append(
                "Only one or zero room-like polygons detected; import may be incomplete."
                " Check for HATCH boundaries, unclosed linework, or that the drawing is a model view."
            )

        schema["_classification_summary"] = self._classification_summary(spaces)
        log.info("[dxf-import] DONE in %.2fs (rooms=%d, connections=%d)",
                 time.monotonic() - t0, len(rooms), len(connections))
        return schema

    def _parse_with_dxfjson(
        self,
        dxf_bytes: bytes,
        filename: str,
        *,
        campus_id: str,
        campus_name: str,
        building_id: str,
        building_name: str,
        floor_id: str,
        floor_index: int,
        floor_display_name: str,
        organization_id: Optional[str] = None,
        organization_name: Optional[str] = None,
        origin_lat: Optional[float] = None,
        origin_lng: Optional[float] = None,
        origin_bearing: float = 0.0,
        layer_mapping: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Attempt to parse the DXF using the Node `dxf-json` parser as a fallback. """
        node_exec = shutil.which("node") or shutil.which("nodejs")
        if not node_exec:
            raise FileNotFoundError("Node.js executable not found on PATH. Install Node.js in the backend image or run the conversion locally.")

        # Locate the helper Node script in parent folders (repo root):
        script_path: Optional[Path] = None
        here = Path(__file__).resolve()
        for p in here.parents:
            cand = p / "scripts" / "dxf_json_parse.mjs"
            if cand.exists():
                script_path = cand
                break
        if script_path is None:
            # Fallback to CWD scripts dir
            cand2 = Path.cwd() / "scripts" / "dxf_json_parse.mjs"
            if cand2.exists():
                script_path = cand2
        if script_path is None:
            raise FileNotFoundError("dxf_json_parse.mjs not found in repository; ensure scripts/dxf_json_parse.mjs is present in the backend image.")

        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp.write(dxf_bytes)
            tmp_path = tmp.name
        try:
            proc = subprocess.run([node_exec, str(script_path), str(tmp_path)], capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise RuntimeError(f"dxf-json parser failed (rc={proc.returncode}): {proc.stderr.strip()[:1000]}")
            parsed = json.loads(proc.stdout)
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

        # Extract closed polylines from the parsed output
        entities = parsed.get("entities") or []
        raw_rooms: list[dict] = []
        for e in entities:
            t = (e.get("type") or "").upper()
            if t not in ("LWPOLYLINE", "POLYLINE"):
                continue
            verts = e.get("vertices") or []
            coords: list[tuple[float, float]] = []
            for v in verts:
                if isinstance(v, dict):
                    x = v.get("x")
                    y = v.get("y")
                elif isinstance(v, (list, tuple)) and len(v) >= 2:
                    x, y = v[0], v[1]
                else:
                    continue
                try:
                    coords.append((float(x), float(y)))
                except Exception:
                    continue
            if not coords:
                continue
            flag = e.get("flag") or e.get("flags") or 0
            closed = False
            try:
                if int(flag) & 1:
                    closed = True
            except Exception:
                closed = False
            if not closed and coords and coords[0] == coords[-1]:
                closed = True
            if not closed:
                continue
            raw_rooms.append({"layer": e.get("layer") or "", "polygon": [(x, y) for x, y in coords]})

        if not raw_rooms:
            # Try polygonizing the raw dxf-json entities (LINE/ARC/CIRCLE)
            raw_rooms = _polygonize_from_parsed(parsed)
            if not raw_rooms:
                raise ValueError("dxf-json produced no closed polylines; no rooms detected")

        rooms, geom_warnings = _validate_and_dedupe(raw_rooms)
        if not rooms:
            raise ValueError("All polygons parsed by dxf-json were rejected as invalid or noise")

        # Extract texts
        texts: list[dict] = []
        for e in entities:
            t = (e.get("type") or "").upper()
            if t == "TEXT":
                pt = e.get("startPoint") or e.get("position") or e.get("insertionPoint")
                raw = e.get("text") or ""
            elif t == "MTEXT":
                pt = e.get("insertionPoint") or e.get("startPoint")
                raw = e.get("text") or ""
            else:
                continue
            if not raw:
                continue
            if isinstance(pt, dict):
                x = pt.get("x")
                y = pt.get("y")
            elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                x, y = pt[0], pt[1]
            else:
                continue
            try:
                texts.append({"x": float(x), "y": float(y), "text": str(raw).strip()})
            except Exception:
                continue

        _attach_labels(rooms, texts)

        # Build spaces list
        spaces: list[dict] = []
        for i, r in enumerate(rooms, start=1):
            sid = f"{floor_id}_space_{i}"
            label = r.get("label") or f"Room {i}"
            polygon = r["polygon"]
            try:
                shape = Polygon(polygon)
                cx, cy = float(shape.centroid.x), float(shape.centroid.y)
                area = float(shape.area)
            except Exception:
                cx = sum(p[0] for p in polygon) / len(polygon)
                cy = sum(p[1] for p in polygon) / len(polygon)
                try:
                    area = float(Polygon(polygon).area)
                except Exception:
                    area = 0.0

            tokens = re.findall(r"\b[\w-]+\b", label)
            short_name: Optional[str] = None
            for t in tokens:
                if any(ch.isdigit() for ch in t):
                    short_name = t
                    break

            tags: list[str] = []
            layer = (r.get("layer") or "").strip()
            if layer:
                tags.append(layer.lower())
            for t in tokens:
                tl = t.lower()
                if len(tl) > 1 and tl not in tags:
                    tags.append(tl)

            spaces.append({
                "id": sid,
                "display_name": label,
                "short_name": short_name,
                "space_type": _classify_layer(layer, overrides=layer_mapping),
                "polygon": [[float(x), float(y)] for x, y in polygon],
                "centroid_x": cx,
                "centroid_y": cy,
                "area_m2": round(area, 3),
                "is_navigable": True,
                "is_accessible": True,
                "is_outdoor": False,
                "tags": tags,
            })

        xs: list[float] = []
        ys: list[float] = []
        for s in spaces:
            for x, y in s.get("polygon", []):
                xs.append(x)
                ys.append(y)
        if xs and ys:
            min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
            building_bounds = [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]
            floor_plan_bounds = building_bounds
        else:
            building_bounds = None
            floor_plan_bounds = None

        schema: dict[str, Any] = {
            "schema_version": "1.0",
            "campus": {
                "id": campus_id,
                "name": campus_name,
                "description": building_name or None,
                "organization_id": organization_id,
                "is_public": False,
                "buildings": [{
                    "id": building_id,
                    "name": building_name,
                    "short_name": (building_name.split()[0] if building_name else None),
                    "organization_id": organization_id,
                    "origin_lat": origin_lat,
                    "origin_lng": origin_lng,
                    "origin_bearing": origin_bearing,
                    "floor_count": 1,
                    "floors": [{
                        "id": floor_id,
                        "floor_index": floor_index,
                        "display_name": floor_display_name,
                        "floor_plan_bounds": floor_plan_bounds,
                        "spaces": spaces,
                    }],
                    "building_bounds": building_bounds,
                }],
                "outdoor_spaces": [],
                "connections": [],
            },
        }
        if organization_id and organization_name:
            schema["organization"] = {
                "id": organization_id,
                "name": organization_name,
                "entity_type": "OTHER",
            }

        schema["_warnings"] = []
        schema["_classification_summary"] = self._classification_summary(spaces)
        return schema

    @staticmethod
    def _classification_summary(spaces: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in spaces:
            out[s["space_type"]] = out.get(s["space_type"], 0) + 1
        return out


def parse_layer_mapping(raw: Optional[str]) -> Optional[dict[str, str]]:
    """Decode the optional `layer_mapping` form field. Returns None
    when the field is absent or empty; raises ValueError on malformed
    JSON or non-string values so the route can surface a 422."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"layer_mapping is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise ValueError("layer_mapping must be a JSON object {layer_name: SpaceType}")
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("layer_mapping keys and values must both be strings")
        out[k] = v
    return out
