"""Pure functions for Wi-Fi indoor positioning."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# A BSSID present in only one of the two vectors contributes this penalty
# (in dBm-equivalent) instead of being ignored, so fingerprints that share
# few APs with the live scan are pushed down the ranking.
_MISSING_AP_PENALTY = 25.0


def _fingerprint_distance(
    scan: Dict[str, float],
    reference: Dict[str, float],
) -> Optional[float]:
    """Euclidean-style distance between two RSSI vectors over the union of
    their BSSIDs. Returns None when they share no AP at all."""
    if not scan or not reference:
        return None
    keys = set(scan) | set(reference)
    shared = set(scan) & set(reference)
    if not shared:
        return None
    total = 0.0
    for bssid in keys:
        if bssid in scan and bssid in reference:
            total += (scan[bssid] - reference[bssid]) ** 2
        else:
            total += _MISSING_AP_PENALTY ** 2
    return math.sqrt(total / len(keys))


def locate_by_rssi(
    scan: Dict[str, float],
    fingerprints: List[dict],
    k: int = 3,
) -> Optional[Tuple[str, float, int]]:
    """Weighted kNN over a floor's fingerprints.

    `fingerprints` is a list of {"space_id", "readings": {bssid: rssi}}.
    Returns (space_id, confidence_0_1, supporting_count) or None.
    """
    scored: List[Tuple[float, str]] = []
    for fp in fingerprints:
        readings = fp.get("readings") or {}
        dist = _fingerprint_distance(scan, readings)
        if dist is None:
            continue
        scored.append((dist, fp["space_id"]))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0])
    top = scored[: max(1, k)]

    # Inverse-distance weighted vote across the k nearest neighbours.
    votes: Dict[str, float] = {}
    for dist, space_id in top:
        weight = 1.0 / (dist + 1e-6)
        votes[space_id] = votes.get(space_id, 0.0) + weight

    best_space = max(votes, key=votes.get)
    support = sum(1 for _, sid in top if sid == best_space)

    # Confidence: how dominant the winning space is among the neighbours,
    # tempered by how close the nearest match actually was.
    total_weight = sum(votes.values())
    dominance = votes[best_space] / total_weight if total_weight else 0.0
    nearest = top[0][0]
    closeness = 1.0 / (1.0 + nearest / 20.0)  # ~1 when nearest≈0, →0 when far
    confidence = max(0.0, min(1.0, 0.5 * dominance + 0.5 * closeness))

    return best_space, confidence, support


def trilaterate_rtt(
    rtt_distances_mm: Dict[str, float],
    access_points: Dict[str, Tuple[float, float]],
) -> Optional[Tuple[float, float]]:
    """Least-squares trilateration of an (x, y) position from FTM ranging.

    `access_points` maps BSSID -> (x, y) floor coordinates. Needs at least
    three APs that are both ranged and positioned; returns None otherwise.
    Dormant in production until AP coordinates are surveyed.
    """
    usable = [
        (access_points[b][0], access_points[b][1], rtt_distances_mm[b] / 1000.0)
        for b in rtt_distances_mm
        if b in access_points
    ]
    if len(usable) < 3:
        return None

    # Linearise around the first anchor (standard multilateration).
    x0, y0, r0 = usable[0]
    a_rows: List[Tuple[float, float]] = []
    b_vals: List[float] = []
    for (xi, yi, ri) in usable[1:]:
        a_rows.append((2 * (xi - x0), 2 * (yi - y0)))
        b_vals.append(
            (r0 ** 2 - ri ** 2) + (xi ** 2 - x0 ** 2) + (yi ** 2 - y0 ** 2)
        )

    # Solve the 2x2 normal equations A^T A p = A^T b.
    saa = sum(ax * ax for ax, _ in a_rows)
    sab = sum(ax * ay for ax, ay in a_rows)
    sbb = sum(ay * ay for _, ay in a_rows)
    sax = sum(ax * bv for (ax, _), bv in zip(a_rows, b_vals))
    say = sum(ay * bv for (_, ay), bv in zip(a_rows, b_vals))

    det = saa * sbb - sab * sab
    if abs(det) < 1e-9:
        return None
    x = (sbb * sax - sab * say) / det
    y = (saa * say - sab * sax) / det
    return x, y
