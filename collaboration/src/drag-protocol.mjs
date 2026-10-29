export function updateDragPosition(drag, x, y) {
  drag.x = x;
  drag.y = y;
  drag.moved = x !== drag.startX || y !== drag.startY;
}

export function dragGrantAction(drag) {
  if (!drag || !drag.ended) return 'continue';
  return drag.moved && !drag.cancelled ? 'commit' : 'release';
}

export function mapObjectCenter(object) {
  if (object.kind !== 'arrow') return { x: object.x, y: object.y };
  const xs = object.points.map((point) => point.x);
  const ys = object.points.map((point) => point.y);
  return { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 };
}
