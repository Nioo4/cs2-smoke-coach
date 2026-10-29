export function sendAuth(socket, ticket) {
  if (typeof ticket !== 'string' || ticket.length < 32) throw new Error('invalid_test_ticket');
  socket.send(JSON.stringify({ type: 'auth', ticket }));
}

export function leaseGrantMessage(requestId, lease) {
  return {
    type: 'lease.granted', request_id: requestId, object_id: lease.objectId, token: lease.token,
    expires_at: lease.expiresAt, renew_every_ms: lease.renewEveryMs,
  };
}

export function leaseRenewedMessage(requestId, lease) {
  return { type: 'lease.renewed', request_id: requestId, object_id: lease.objectId, expires_at: lease.expiresAt, renew_every_ms: lease.renewEveryMs };
}

export function parseLeaseGrant(message) {
  if (message?.type !== 'lease.granted' || typeof message.request_id !== 'string' ||
      typeof message.object_id !== 'string' || typeof message.token !== 'string' ||
      !Number.isFinite(message.expires_at) || !Number.isSafeInteger(message.renew_every_ms) || message.renew_every_ms < 1) return null;
  return {
    requestId: message.request_id, objectId: message.object_id, token: message.token,
    expiresAt: message.expires_at, renewEveryMs: message.renew_every_ms,
  };
}

export function mapMoveOperation({ operationId, epoch, drag, lease }) {
  return {
    type: 'map.move', operation_id: operationId, epoch,
    payload: { step_id: drag.stepId, object_id: drag.objectId, token: lease.token, version: drag.version, x: drag.x, y: drag.y },
  };
}

export function shouldFinishLocalMapMove(message, wasPending) {
  return wasPending && message?.operation_type === 'map.move';
}

export function settleProtocolError(code, hasDrag, finishMove, flash) {
  if (hasDrag) finishMove();
  flash(code === 'COLLAB_LOCKED' ? '该对象正由另一位参与者拖动。' : `操作未完成：${code}`);
}

export function hasUnconfirmedWork(state) {
  return state.pending.size > 0 || state.localUnconfirmed || state.rejectedLocal ||
    state.recoveryDraft !== null || state.failedOperations.length > 0 || state.composing.size > 0;
}
