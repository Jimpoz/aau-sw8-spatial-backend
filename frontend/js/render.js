/* ═══════════════════════════════════════════════
   Canvas Rendering
   ═══════════════════════════════════════════════ */
function resizeCanvas() {
  const container = $('map-container');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = container.clientWidth * dpr;
  canvas.height = container.clientHeight * dpr;
  canvas.style.width = container.clientWidth + 'px';
  canvas.style.height = container.clientHeight + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function render() {
  const w = canvas.width / (window.devicePixelRatio || 1);
  const h = canvas.height / (window.devicePixelRatio || 1);
  ctx.clearRect(0, 0, w, h);

  if (!state.bounds) return;

  const effScale = state.baseScale * state.zoom;
  const showLabels = effScale > 2;

  // Draw infinite grid based on viewport
  if (state.selectedFloorId) {
    ctx.save();
    ctx.strokeStyle = '#e8e8e8';
    ctx.lineWidth = 0.5;
    ctx.font = '9px sans-serif';
    ctx.fillStyle = '#ccc';
    const gridStep = 5;
    const [vMinX, vMaxY] = canvasToWorld(0, 0);
    const [vMaxX, vMinY] = canvasToWorld(w, h);
    const startX = Math.floor(vMinX / gridStep) * gridStep;
    const startY = Math.floor(vMinY / gridStep) * gridStep;
    for (let x = startX; x <= vMaxX + gridStep; x += gridStep) {
      const [cx1] = worldToCanvas(x, vMinY);
      const [cx2] = worldToCanvas(x, vMaxY);
      ctx.beginPath(); ctx.moveTo(cx1, 0); ctx.lineTo(cx2, h); ctx.stroke();
      if (effScale > 1.5) ctx.fillText(`${x}m`, cx1 + 2, h - 4);
    }
    for (let y = startY; y <= vMaxY + gridStep; y += gridStep) {
      const [, cy1] = worldToCanvas(vMinX, y);
      const [, cy2] = worldToCanvas(vMaxX, y);
      ctx.beginPath(); ctx.moveTo(0, cy1); ctx.lineTo(w, cy2); ctx.stroke();
      if (effScale > 1.5) ctx.fillText(`${y}m`, 4, cy1 - 2);
    }
    ctx.restore();
  }

  // Draw spaces
  for (const space of state.spaces) {
    const isConnType = space.space_type && (
      space.space_type.startsWith('DOOR_') || space.space_type === 'PASSAGE'
    );

    if (!space.polygon || space.polygon.length < 3) {
      // Draw connection nodes (doors/passages) as small rectangles at centroid
      if (isConnType && space.centroid_x != null && space.centroid_y != null) {
        const [cx, cy] = worldToCanvas(space.centroid_x, space.centroid_y);
        const size = Math.max(4, effScale * 0.6);
        const fill = COLORS[space.space_type] || '#D4A574';
        ctx.save();
        ctx.fillStyle = fill;
        ctx.strokeStyle = '#999';
        ctx.lineWidth = 0.5;
        ctx.fillRect(cx - size, cy - size/2, size * 2, size);
        ctx.strokeRect(cx - size, cy - size/2, size * 2, size);
        if (showLabels) {
          const fontSize = Math.min(10, Math.max(7, effScale * 0.5));
          ctx.font = `${fontSize}px -apple-system, sans-serif`;
          ctx.fillStyle = '#666';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'top';
          ctx.fillText(space.display_name || '', cx, cy + size/2 + 2);
        }
        ctx.restore();
      }
      continue;
    }

    const isHovered = state.hoveredSpace && state.hoveredSpace.id === space.id;
    const isConnectFrom = state.mode === 'connect' && state.connectFrom && state.connectFrom.id === space.id;

    let fill = COLORS[space.space_type] || '#F0F0F0';
    if (isHovered) fill = blendColor(fill, '#000000', 0.1);

    const isSubspace = (space.z_index || 0) > 0;
    drawPolygon(space.polygon, fill, isSubspace ? '#666' : '#999', isSubspace ? 1.0 : 0.5);

    if (isConnectFrom) drawPolygon(space.polygon, null, '#ff9500', 3);

    // Labels
    if (showLabels && space.centroid_x != null && space.centroid_y != null) {
      const [lx, ly] = worldToCanvas(space.centroid_x, space.centroid_y);
      ctx.save();
      const fontSize = Math.min(14, Math.max(9, effScale * 0.8));
      ctx.font = `${fontSize}px -apple-system, sans-serif`;
      ctx.fillStyle = '#333';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const name = space.display_name || '';
      const maxW = effScale * 3;
      ctx.fillText(name.length > 20 ? name.slice(0, 18) + '..' : name, lx, ly, maxW);
      ctx.restore();
    }

    // Vertical connection indicators
    if (effScale > 1 && space.vertical_connections && space.vertical_connections.length > 0 && state.currentFloorIndex != null) {
      const vc = space.vertical_connections;
      const hasUp = vc.some(c => c.to_floor_index > state.currentFloorIndex);
      const hasDown = vc.some(c => c.to_floor_index < state.currentFloorIndex);
      if (hasUp || hasDown) {
        // Compute bounding box top-right from polygon
        let maxX = -Infinity, minY = Infinity;
        for (const pt of space.polygon) { if (pt[0] > maxX) maxX = pt[0]; if (pt[1] < minY) minY = pt[1]; }
        const [bx, by] = worldToCanvas(maxX, minY);
        const label = (hasUp && hasDown) ? '▲▼' : hasUp ? '▲' : '▼';
        const fs = Math.min(12, Math.max(8, effScale * 0.7));
        ctx.save();
        ctx.font = `bold ${fs}px sans-serif`;
        ctx.fillStyle = '#FF6B00';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillText(label, bx - 2, by + 2);
        ctx.restore();
      }
    }
  }

  // Draw connection arrows
  for (const conn of state.connections) {
    if (conn.from_cx == null || conn.from_cy == null || conn.to_cx == null || conn.to_cy == null) continue;
    const [x1, y1] = worldToCanvas(conn.from_cx, conn.from_cy);
    const [x2, y2] = worldToCanvas(conn.to_cx, conn.to_cy);
    const dx = x2 - x1, dy = y2 - y1;
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist < 10) continue;

    // Shorten line: 30% from each end, max 20px
    const shrink = Math.min(dist * 0.3, 20);
    const ux = dx / dist, uy = dy / dist;
    const sx = x1 + ux * shrink, sy = y1 + uy * shrink;
    const ex = x2 - ux * shrink, ey = y2 - uy * shrink;

    ctx.save();
    ctx.strokeStyle = 'rgba(150,150,150,0.5)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(sx, sy);
    ctx.lineTo(ex, ey);
    ctx.stroke();

    // Arrowhead at target end
    ctx.fillStyle = 'rgba(150,150,150,0.7)';
    const angle = Math.atan2(ey - sy, ex - sx);
    ctx.translate(ex, ey);
    ctx.rotate(angle);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-8, -4);
    ctx.lineTo(-8, 4);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  // Draw snap indicator lines
  if (state.snapLines.length > 0) {
    ctx.save();
    ctx.strokeStyle = '#00e5ff';
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 4]);
    for (const line of state.snapLines) {
      if (line.axis === 'x') {
        const [cx] = worldToCanvas(line.value, 0);
        ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke();
      } else {
        const [, cy] = worldToCanvas(0, line.value);
        ctx.beginPath(); ctx.moveTo(0, cy); ctx.lineTo(w, cy); ctx.stroke();
      }
    }
    ctx.restore();
  }

  // In-progress polygon (addSpace + polygon shape): placed vertices + edges,
  // plus a dashed rubber-band segment from the last vertex to the cursor.
  if (state.mode === 'addSpace' && state.pendingPolygon && state.pendingPolygon.length > 0) {
    const color = COLORS[$('new-space-type').value] || '#4a90d9';
    ctx.save();

    // Placed edges (solid)
    if (state.pendingPolygon.length >= 2) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      const [sx, sy] = worldToCanvas(state.pendingPolygon[0][0], state.pendingPolygon[0][1]);
      ctx.moveTo(sx, sy);
      for (let i = 1; i < state.pendingPolygon.length; i++) {
        const [px, py] = worldToCanvas(state.pendingPolygon[i][0], state.pendingPolygon[i][1]);
        ctx.lineTo(px, py);
      }
      ctx.stroke();
    }

    // Rubber-band preview to cursor (dashed), only when cursor known
    if (state.pendingCursorWorld) {
      const last = state.pendingPolygon[state.pendingPolygon.length - 1];
      const [lx, ly] = worldToCanvas(last[0], last[1]);
      const [cx, cy] = worldToCanvas(state.pendingCursorWorld[0], state.pendingCursorWorld[1]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(lx, ly);
      ctx.lineTo(cx, cy);
      ctx.stroke();
      ctx.setLineDash([]);

      // Closing hint: dashed line back to first vertex if >= 3 points
      if (state.pendingPolygon.length >= 3) {
        const [fx, fy] = worldToCanvas(state.pendingPolygon[0][0], state.pendingPolygon[0][1]);
        ctx.strokeStyle = 'rgba(74,144,217,0.5)';
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(fx, fy);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // Vertex handles
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    for (let i = 0; i < state.pendingPolygon.length; i++) {
      const [px, py] = worldToCanvas(state.pendingPolygon[i][0], state.pendingPolygon[i][1]);
      ctx.beginPath();
      ctx.arc(px, py, i === 0 ? 5 : 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }
}

function drawPolygon(polygon, fill, stroke, lineWidth) {
  ctx.beginPath();
  const [mx, my] = worldToCanvas(polygon[0][0], polygon[0][1]);
  ctx.moveTo(mx, my);
  for (let i = 1; i < polygon.length; i++) {
    const [px, py] = worldToCanvas(polygon[i][0], polygon[i][1]);
    ctx.lineTo(px, py);
  }
  ctx.closePath();
  if (fill) { ctx.fillStyle = fill; ctx.fill(); }
  if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lineWidth || 1; ctx.stroke(); }
}

function blendColor(hex, overlay, alpha) {
  const parse = h => [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)];
  const [r1,g1,b1] = parse(hex);
  const [r2,g2,b2] = parse(overlay);
  const r = Math.round(r1*(1-alpha) + r2*alpha);
  const g = Math.round(g1*(1-alpha) + g2*alpha);
  const b = Math.round(b1*(1-alpha) + b2*alpha);
  return `rgb(${r},${g},${b})`;
}
