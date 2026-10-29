export function createSocketAuthState() {
  return { closed: false, authenticating: false, connection: null };
}

export async function authenticateSocket({ core, socket, state, connectionId, actor, documentId, send }) {
  if (state.closed || state.authenticating || state.connection) return null;
  state.authenticating = true;
  try {
    const connection = await core.connect({ connectionId, actor, documentId, send });
    if (state.closed || socket.readyState !== socket.OPEN) {
      core.disconnect(connection);
      return null;
    }
    state.connection = connection;
    return connection;
  } finally {
    state.authenticating = false;
  }
}

export function closeSocketAuth(core, state) {
  if (state.closed) return;
  state.closed = true;
  const connection = state.connection;
  state.connection = null;
  if (connection) core.disconnect(connection);
}
