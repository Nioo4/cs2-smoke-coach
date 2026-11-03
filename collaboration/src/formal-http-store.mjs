import { randomUUID } from 'node:crypto';

export class FormalError extends Error {
  constructor(code, details = {}) { super(code); this.code = code; this.details = details; }
}

export class FormalHttpStore {
  constructor({ baseUrl, internalKey, timeoutMs = 5_000, instanceId = randomUUID() }) {
    const url = new URL(baseUrl);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('invalid_formal_backend_url');
    if (typeof internalKey !== 'string' || Buffer.byteLength(internalKey) < 32) throw new Error('formal_internal_key_required');
    this.baseUrl = url.origin; this.internalKey = internalKey; this.timeoutMs = timeoutMs; this.instanceId = instanceId;
  }

  async call(path, body, timeoutMs = this.timeoutMs) {
    let result;
    try {
      result = await fetch(`${this.baseUrl}/api/v1/internal/tactics${path}`, {
        method: 'POST', headers: { 'content-type': 'application/json', 'internal-key': this.internalKey },
        body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs),
      });
    } catch { throw new FormalError('COLLAB_UNAVAILABLE'); }
    let data;
    try { data = await result.json(); } catch { throw new FormalError('COLLAB_UNAVAILABLE'); }
    if (!result.ok) throw new FormalError(data.error?.code ?? 'COLLAB_UNAVAILABLE', data.error?.details);
    return data;
  }

  consume(ticket, connectionId) { return this.call('/tickets/consume', { ticket, connection_id: connectionId, node_instance_id: this.instanceId }); }
  load(documentId, context) { return this.call(`/${documentId}/load`, { grant_id: context.grant_id }); }
  findOperation(documentId, epoch, operationId, context) { return this.call(`/${documentId}/operation-lookup`, { grant_id: context.grant_id, epoch, operation_id: operationId }).then((data) => data.operation); }
  persist(documentId, context, body) { return this.call(`/${documentId}/commit`, { grant_id: context.grant_id, ...body }); }
  readRecovery(documentId, recoveryId, context) { return this.call(`/${documentId}/recoveries/${recoveryId}/read`, { grant_id: context.grant_id }); }
  checkGrants(grantIds) { return this.call('/grants/check', { grant_ids: grantIds }, 2_000).then((data) => data.grants); }
  release(context) { return this.call(`/grants/${context.grant_id}/release`, {}); }
}
