export function moveCommandFromDrawing(drawing) {
  if (drawing?.mode !== 'new-move' || !drawing.command || !Array.isArray(drawing.points)) throw new Error('invalid_move_drawing');
  return { ...drawing.command, role_id: drawing.roleId, path: structuredClone(drawing.points) };
}

export function mapClickIntent(tool, drawing) {
  if (drawing) return 'drawing';
  return tool === 'position' ? 'position' : 'select';
}

export function mapPointFromRect(clientX, clientY, rect) {
  if (!rect?.width || !rect?.height) throw new Error('invalid_map_bounds');
  return {
    x: Math.max(0, Math.min(100, (clientX - rect.left) / rect.width * 100)),
    y: Math.max(0, Math.min(100, (clientY - rect.top) / rect.height * 100)),
  };
}
