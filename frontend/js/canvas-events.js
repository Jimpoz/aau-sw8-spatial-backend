/* ═══════════════════════════════════════════════
   Mouse Interaction
   ═══════════════════════════════════════════════ */
canvas.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  if (state.mode === 'moveSpace') return;
  state.isDragging = true;
  state.dragStart = { x: e.clientX, y: e.clientY };
  state.panStart = { ...state.pan };
  canvas.classList.add('dragging');
});

canvas.addEventListener('dblclick', e => {
  if (state.mode !== 'addSpace' || currentShape() !== 'polygon') return;
  if (state.pendingPolygon.length < 3) return;
  e.preventDefault();
  const last = state.pendingPolygon[state.pendingPolygon.length - 1];
  const prev = state.pendingPolygon[state.pendingPolygon.length - 2];
  if (last && prev && last[0] === prev[0] && last[1] === prev[1]) {
    state.pendingPolygon.pop();
  }
  finishPendingPolygon();
});

canvas.addEventListener('mousemove', e => {
  if (state.isDragging) {
    state.pan.x = state.panStart.x + (e.clientX - state.dragStart.x);
    state.pan.y = state.panStart.y + (e.clientY - state.dragStart.y);
    render();
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;

  // Move mode: update space position with snapping
  if (state.mode === 'moveSpace' && state.moveSpace) {
    const [rawWx, rawWy] = canvasToWorld(mx, my);
    if (state.moveOrigPoly) {
      const origBB = getSpaceBBox({ polygon: state.moveOrigPoly });
      const halfW = (origBB.maxX - origBB.minX) / 2;
      const halfH = (origBB.maxY - origBB.minY) / 2;
      const [sx, sy] = applySnap(rawWx, rawWy, halfW, halfH, state.moveSpace.id);
      const dx = sx - state.moveOrigCentroid[0];
      const dy = sy - state.moveOrigCentroid[1];
      state.moveSpace.polygon = state.moveOrigPoly.map(([px, py]) => [px + dx, py + dy]);
      state.moveSpace.centroid_x = sx;
      state.moveSpace.centroid_y = sy;
    } else {
      state.moveSpace.centroid_x = rawWx;
      state.moveSpace.centroid_y = rawWy;
    }
    render();
    return;
  }

  // Duplicate placement mode: ghost polygon follows cursor
  if (state.mode === 'placeDuplicate' && state.pendingDuplicate) {
    const [rawWx, rawWy] = canvasToWorld(mx, my);
    const relPoly = state.pendingDupOrigPoly;
    const bb = getSpaceBBox({ polygon: relPoly.map(([rx, ry]) => [rx + rawWx, ry + rawWy]) });
    const halfW = (bb.maxX - bb.minX) / 2;
    const halfH = (bb.maxY - bb.minY) / 2;
    const [sx, sy] = applySnap(rawWx, rawWy, halfW, halfH, null);
    state.pendingDuplicate.centroid_x = sx;
    state.pendingDuplicate.centroid_y = sy;
    state.pendingDuplicate.polygon = relPoly.map(([rx, ry]) => [rx + sx, ry + sy]);
    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX - rect.left + 14) + 'px';
    tooltip.style.top = (e.clientY - rect.top - 10) + 'px';
    tooltip.textContent = `${sx.toFixed(1)}, ${sy.toFixed(1)} m`;
    render();
    const color = COLORS[state.pendingDuplicate.space_type] || '#F0F0F0';
    drawPolygon(state.pendingDuplicate.polygon, blendColor(color, '#ffffff', 0.4), '#4a90d9', 2);
    return;
  }

  const space = hitTest(mx, my);
  if (space !== state.hoveredSpace) {
    state.hoveredSpace = space;
    render();
  }
  if (state.mode === 'addSpace' && state.selectedFloorId) {
    const [rawWx, rawWy] = canvasToWorld(mx, my);
    const shape = currentShape();

    let pendX = rawWx, pendY = rawWy;
    if (shape === 'polygon' && state.pendingPolygon.length > 0) {
      const last = state.pendingPolygon[state.pendingPolygon.length - 1];
      [pendX, pendY] = snapToAxis(last[0], last[1], rawWx, rawWy);
    }
    state.pendingCursorWorld = [pendX, pendY];
    let ghostPoly = null;
    let tipX = pendX, tipY = pendY;

    if (shape === 'rectangle') {
      const w = parseFloat($('new-space-w').value) || 4;
      const h = parseFloat($('new-space-h').value) || 3;
      const halfW = w / 2, halfH = h / 2;
      const [wx, wy] = applySnap(rawWx, rawWy, halfW, halfH, null);
      tipX = wx; tipY = wy;
      ghostPoly = [
        [wx - halfW, wy - halfH], [wx + halfW, wy - halfH],
        [wx + halfW, wy + halfH], [wx - halfW, wy + halfH], [wx - halfW, wy - halfH],
      ];
    } else if (shape === 'circle') {
      const r = parseFloat($('new-space-radius').value) || 2;
      ghostPoly = approximateCircle(rawWx, rawWy, r, 32);
    }

    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX - rect.left + 14) + 'px';
    tooltip.style.top = (e.clientY - rect.top - 10) + 'px';
    tooltip.textContent = `${tipX.toFixed(1)}, ${tipY.toFixed(1)} m`;
    render();

    if (ghostPoly) {
      const color = COLORS[$('new-space-type').value] || '#F0F0F0';
      drawPolygon(ghostPoly, blendColor(color, '#ffffff', 0.4), '#4a90d9', 2);
    }
  } else if (space) {
    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX - rect.left + 14) + 'px';
    tooltip.style.top = (e.clientY - rect.top - 10) + 'px';
    tooltip.textContent = `${space.display_name} (${space.space_type.replace(/_/g, ' ').toLowerCase()})`;
  } else {
    state.snapLines = [];
    tooltip.style.display = 'none';
  }
});

canvas.addEventListener('mouseup', e => {
  if (e.button !== 0) return;
  hideContextMenu();

  // Move mode: click to place
  if (state.mode === 'moveSpace' && state.moveSpace) {
    finishMove();
    return;
  }

  // Duplicate placement mode: click to place
  if (state.mode === 'placeDuplicate' && state.pendingDuplicate) {
    finishDuplicatePlace();
    return;
  }

  const wasDrag = Math.abs(e.clientX - state.dragStart.x) > 3 || Math.abs(e.clientY - state.dragStart.y) > 3;
  state.isDragging = false;
  canvas.classList.remove('dragging');
  if (wasDrag) return;

  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;

  if (state.mode === 'addSpace') {
    handleAddSpaceClick(cx, cy);
    return;
  }

  if (state.mode === 'connect') {
    handleConnectClick(cx, cy);
    return;
  }

  // In select mode, clicking a space is a no-op
});

canvas.addEventListener('mouseleave', () => {
  state.isDragging = false;
  canvas.classList.remove('dragging');
  tooltip.style.display = 'none';
  if (state.hoveredSpace) { state.hoveredSpace = null; render(); }
});

// Right-click context menu
canvas.addEventListener('contextmenu', e => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  const space = hitTest(cx, cy);
  if (!space) { hideContextMenu(); return; }
  state.contextMenu = { space, x: cx, y: cy };
  showContextMenu(cx, cy, rect);
});

canvas.addEventListener('wheel', e => {
  e.preventDefault();
  hideContextMenu();
  const factor = e.deltaY > 0 ? 0.9 : 1.1;
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  state.pan.x = mx - (mx - state.pan.x) * factor;
  state.pan.y = my - (my - state.pan.y) * factor;
  state.zoom *= factor;
  render();
}, { passive: false });
