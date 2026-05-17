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
_MAX_ROOM_AREA_M2 = 5000.0
_LABEL_BOUNDARY_BUFFER_M = 0.1
_NEAREST_LABEL_FALLBACK_M = 1.0       # tight leader-line tolerance — broad fallback was adopting random text onto slivers
_MIN_ROOM_AREA_FOR_LABEL_M2 = 2.0     # below this, a polygon won't steal a contained label
_MIN_ROOM_SHORT_SIDE_M = 0.6
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


def _polygon_area_m2(coords) -> float:
    """Best-effort polygon area; returns 0 on construction failure."""
    try:
        return float(Polygon(coords).area)
    except Exception:
        return 0.0


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
    min_room_area_for_label_m2: float = _MIN_ROOM_AREA_FOR_LABEL_M2,
    overrides: Optional[dict[str, str]] = None,
) -> tuple[list[dict], list[str], dict[str, int]]:
    """
    Attach labels to rooms and drop everything unlabeled.
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

    overrides_upper = {
        (k or "").upper(): v for k, v in (overrides or {}).items() if k
    }

    kept: list[dict] = []
    stats["dropped_unlabeled"] = 0
    stats["kept_via_layer_override"] = 0
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
            layer = (rec.get("layer") or "").upper()
            if layer and layer in overrides_upper:
                meta["label_source"] = "layer_override"
                meta["layer_override_type"] = overrides_upper[layer]
                stats["kept_via_layer_override"] += 1
            else:
                stats["dropped_unlabeled"] += 1
                continue
        rec["metadata"] = meta
        kept.append(rec)

    if stats["dropped_unlabeled"]:
        warnings.append(
            f"Dropped {stats['dropped_unlabeled']} unlabeled polygons "
            f"(no DXF text inside, no layer override matched). "
            f"To rescue an intentionally-unlabeled area, add its layer "
            f"name to the layer_mapping form field."
        )
    if stats["kept_with_external_label"]:
        warnings.append(
            f"{stats['kept_with_external_label']} rooms attached labels via "
            f"nearest-external fallback (within {nearest_fallback_m:.1f} m of centroid)."
        )
    if stats["kept_via_layer_override"]:
        warnings.append(
            f"{stats['kept_via_layer_override']} unlabeled polygons kept "
            f"on the strength of a user-supplied layer override."
        )
    return kept, warnings, stats


def _drop_slivers(
    rooms: list[dict],
    *,
    min_short_side_m: float = _MIN_ROOM_SHORT_SIDE_M,
) -> tuple[list[dict], int]:
    """Drop polygons whose minimum-rotated-rectangle short side is below
    `min_short_side_m` — these are likely wall slivers, not real rooms."""
    if not rooms:
        return rooms, 0
    kept: list[dict] = []
    dropped = 0
    for r in rooms:
        coords = r.get("polygon") or []
        if len(coords) < 3:
            continue
        short_side: Optional[float] = None
        try:
            poly = Polygon(coords)
            mrr = poly.minimum_rotated_rectangle
            ring = list(mrr.exterior.coords)
            if len(ring) >= 3:
                side_a = math.hypot(ring[1][0] - ring[0][0], ring[1][1] - ring[0][1])
                side_b = math.hypot(ring[2][0] - ring[1][0], ring[2][1] - ring[1][1])
                short_side = min(side_a, side_b)
        except Exception:
            short_side = None
        if short_side is None:
            xs = [p[0] for p in coords]
            ys = [p[1] for p in coords]
            short_side = min(max(xs) - min(xs), max(ys) - min(ys))
        if short_side < min_short_side_m:
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


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
        if rooms[i].get("label"):
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


def _arc_is_door_candidate(radius_m: float, sweep_deg: float) -> bool:
    """A door swing has a characteristic geometry: leaf-width radius
    (0.4-1.5 m) and a 45-130 deg arc sweep. Round tables, decorative
    fillets and very long curves are filtered out."""
    if not (_DOOR_ARC_RADIUS_RANGE_M[0] <= radius_m <= _DOOR_ARC_RADIUS_RANGE_M[1]):
        return False
    return _DOOR_ARC_SWEEP_DEG[0] <= sweep_deg <= _DOOR_ARC_SWEEP_DEG[1]


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
