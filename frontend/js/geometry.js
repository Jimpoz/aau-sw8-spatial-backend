/* ═══════════════════════════════════════════════
   Coordinate Transform
   ═══════════════════════════════════════════════ */
function computeBounds(spaces) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const s of spaces) {
    if (!s.polygon) continue;
    for (const [x, y] of s.polygon) {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
  }
  if (minX === Infinity) return null;
  return { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };
}

function fitToCanvas() {
  let b = computeBounds(state.spaces);
  if (!b || b.width === 0 || b.height === 0) {
    b = { minX: 0, minY: 0, maxX: 50, maxY: 50, width: 50, height: 50 };
  }
  state.bounds = b;
  const pad = 40;
  const cw = canvas.width - pad * 2;
  const ch = canvas.height - pad * 2;
  state.baseScale = Math.min(cw / b.width, ch / b.height);
  state.zoom = 1;
  const scaledW = b.width * state.baseScale;
  const scaledH = b.height * state.baseScale;
  state.pan.x = (canvas.width - scaledW) / 2;
  state.pan.y = (canvas.height - scaledH) / 2;
}

function worldToCanvas(wx, wy) {
  const b = state.bounds;
  if (!b) return [0, 0];
  const s = state.baseScale * state.zoom;
  const cx = (wx - b.minX) * s + state.pan.x;
  const cy = (b.maxY - wy) * s + state.pan.y;
  return [cx, cy];
}

function canvasToWorld(cx, cy) {
  const b = state.bounds;
  if (!b) return [0, 0];
  const s = state.baseScale * state.zoom;
  const wx = (cx - state.pan.x) / s + b.minX;
  const wy = b.maxY - (cy - state.pan.y) / s;
  return [wx, wy];
}

/* ═══════════════════════════════════════════════
   Snapping
   ═══════════════════════════════════════════════ */
function gridSnap(value, step = 1) {
  return Math.round(value / step) * step;
}

function getSpaceBBox(space) {
  if (!space.polygon || space.polygon.length < 3) return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of space.polygon) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  return { minX, minY, maxX, maxY };
}

function edgeSnap(wx, wy, halfW, halfH, excludeId) {
  const THRESH = 1.5;
  let snapX = null, snapY = null;
  const myLeft = wx - halfW, myRight = wx + halfW;
  const myBot = wy - halfH, myTop = wy + halfH;
  let bestDx = THRESH, bestDy = THRESH;

  for (const s of state.spaces) {
    if (s.id === excludeId) continue;
    const bb = getSpaceBBox(s);
    if (!bb) continue;
    const overlapY = myTop > bb.minY && myBot < bb.maxY;
    const overlapX = myRight > bb.minX && myLeft < bb.maxX;

    if (overlapY) {
      const d1 = Math.abs(myRight - bb.minX);
      if (d1 < bestDx) { bestDx = d1; snapX = bb.minX - halfW; }
      const d2 = Math.abs(myLeft - bb.maxX);
      if (d2 < bestDx) { bestDx = d2; snapX = bb.maxX + halfW; }
      const d3 = Math.abs(myRight - bb.maxX);
      if (d3 < bestDx) { bestDx = d3; snapX = bb.maxX - halfW; }
      const d4 = Math.abs(myLeft - bb.minX);
      if (d4 < bestDx) { bestDx = d4; snapX = bb.minX + halfW; }
    }
    if (overlapX) {
      const d5 = Math.abs(myTop - bb.minY);
      if (d5 < bestDy) { bestDy = d5; snapY = bb.minY - halfH; }
      const d6 = Math.abs(myBot - bb.maxY);
      if (d6 < bestDy) { bestDy = d6; snapY = bb.maxY + halfH; }
      const d7 = Math.abs(myTop - bb.maxY);
      if (d7 < bestDy) { bestDy = d7; snapY = bb.maxY - halfH; }
      const d8 = Math.abs(myBot - bb.minY);
      if (d8 < bestDy) { bestDy = d8; snapY = bb.minY + halfH; }
    }
  }
  return { snapX, snapY };
}

function applySnap(wx, wy, halfW, halfH, excludeId) {
  let sx = gridSnap(wx), sy = gridSnap(wy);
  const edge = edgeSnap(sx, sy, halfW, halfH, excludeId);
  const lines = [];
  if (edge.snapX !== null) {
    sx = edge.snapX;
    lines.push({ axis: 'x', value: sx - halfW }, { axis: 'x', value: sx + halfW });
  }
  if (edge.snapY !== null) {
    sy = edge.snapY;
    lines.push({ axis: 'y', value: sy - halfH }, { axis: 'y', value: sy + halfH });
  }
  state.snapLines = lines;
  return [sx, sy];
}

/* ═══════════════════════════════════════════════
   Hit Testing
   ═══════════════════════════════════════════════ */
function pointInPolygon(px, py, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = polygon[i][0], yi = polygon[i][1];
    const xj = polygon[j][0], yj = polygon[j][1];
    if ((yi > py) !== (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function hitTest(canvasX, canvasY) {
  const [wx, wy] = canvasToWorld(canvasX, canvasY);
  // Check door/passage nodes by centroid proximity
  for (let i = state.spaces.length - 1; i >= 0; i--) {
    const s = state.spaces[i];
    if ((!s.polygon || s.polygon.length < 3) &&
        s.space_type && (s.space_type.startsWith('DOOR_') || s.space_type === 'PASSAGE') &&
        s.centroid_x != null && s.centroid_y != null) {
      const dx = Math.abs(wx - s.centroid_x);
      const dy = Math.abs(wy - s.centroid_y);
      if (dx < 1.5 && dy < 1.0) return s;
    }
  }
  // Existing polygon hit test
  for (let i = state.spaces.length - 1; i >= 0; i--) {
    const s = state.spaces[i];
    if (!s.polygon || s.polygon.length < 3) continue;
    if (pointInPolygon(wx, wy, s.polygon)) return s;
  }
  return null;
}

function approximateCircle(cx, cy, radius, segments = 32) {
  const pts = [];
  for (let i = 0; i < segments; i++) {
    const angle = (i / segments) * Math.PI * 2;
    pts.push([cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius]);
  }
  pts.push([...pts[0]]); // close
  return pts;
}

function polygonCentroid(polygon) {
  let area = 0, cx = 0, cy = 0;
  const n = polygon.length - (
    polygon[0][0] === polygon[polygon.length - 1][0] &&
    polygon[0][1] === polygon[polygon.length - 1][1] ? 1 : 0
  );
  for (let i = 0; i < n; i++) {
    const [x1, y1] = polygon[i];
    const [x2, y2] = polygon[(i + 1) % n];
    const cross = x1 * y2 - x2 * y1;
    area += cross;
    cx += (x1 + x2) * cross;
    cy += (y1 + y2) * cross;
  }
  area /= 2;
  if (area === 0) {
    const mean = polygon.reduce((a, [x, y]) => [a[0] + x, a[1] + y], [0, 0]);
    return [mean[0] / polygon.length, mean[1] / polygon.length];
  }
  return [cx / (6 * area), cy / (6 * area)];
}

function snapToAxis(prevX, prevY, cursorX, cursorY, thresholdDeg = 10) {
  const dx = cursorX - prevX;
  const dy = cursorY - prevY;
  const len = Math.hypot(dx, dy);
  if (len < 0.01) return [cursorX, cursorY];

  const thresholdRad = thresholdDeg * Math.PI / 180;
  const angle = Math.atan2(dy, dx);
  // 8 snap angles spaced every 45°
  const snaps = [0, 1, 2, 3, 4, -1, -2, -3].map(k => k * Math.PI / 4);

  let best = null, bestDiff = Infinity;
  for (const sa of snaps) {
    let diff = Math.abs(angle - sa);
    if (diff > Math.PI) diff = 2 * Math.PI - diff;
    if (diff < bestDiff) { bestDiff = diff; best = sa; }
  }
  if (bestDiff < thresholdRad) {
    if (best === 0 || best === Math.PI || best === -Math.PI) return [cursorX, prevY];
    if (best === Math.PI / 2 || best === -Math.PI / 2) return [prevX, cursorY];
    return [prevX + Math.cos(best) * len, prevY + Math.sin(best) * len];
  }
  return [cursorX, cursorY];
}

function closedPolygon(points) {
  if (points.length === 0) return [];
  const first = points[0], last = points[points.length - 1];
  if (first[0] === last[0] && first[1] === last[1]) return points;
  return [...points, [...first]];
}
