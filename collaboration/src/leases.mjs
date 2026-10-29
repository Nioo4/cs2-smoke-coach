import { randomUUID } from 'node:crypto';

export class LeaseError extends Error {
  constructor(code, details = {}) {
    super(code);
    this.code = code;
    this.details = details;
  }
}

export class LeaseManager {
  constructor({ now = Date.now, leaseMs = 10_000, renewalMs = 3_000 } = {}) {
    this.now = now;
    this.leaseMs = leaseMs;
    this.renewalMs = renewalMs;
    this.leases = new Map();
    this.lastPreviewAt = new Map();
  }

  sweep() {
    const now = this.now();
    for (const [objectId, lease] of this.leases) {
      if (lease.expiresAt <= now) this.leases.delete(objectId);
    }
  }

  acquire({ objectId, version, connectionId, actor, currentVersion }) {
    this.sweep();
    const lease = this.leases.get(objectId);
    if (lease && lease.connectionId !== connectionId) throw new LeaseError('COLLAB_LOCKED', { owner: lease.actor.name, color: lease.actor.color });
    if (version !== currentVersion) throw new LeaseError('TACTIC_CONFLICT');
    const next = lease ?? { objectId, connectionId, actor, token: randomUUID(), acquiredAt: this.now() };
    next.expiresAt = this.now() + this.leaseMs;
    this.leases.set(objectId, next);
    return { objectId, token: next.token, expiresAt: next.expiresAt, renewEveryMs: this.renewalMs };
  }

  renew({ objectId, token, connectionId }) {
    this.sweep();
    const lease = this.leases.get(objectId);
    if (!lease || lease.connectionId !== connectionId || lease.token !== token) throw new LeaseError('LEASE_EXPIRED');
    lease.expiresAt = this.now() + this.leaseMs;
    return { objectId, expiresAt: lease.expiresAt, renewEveryMs: this.renewalMs };
  }

  assertHeld({ objectId, token, connectionId, version, currentVersion }) {
    this.sweep();
    const lease = this.leases.get(objectId);
    if (!lease || lease.connectionId !== connectionId || lease.token !== token) throw new LeaseError('COLLAB_LOCKED');
    if (version !== currentVersion) throw new LeaseError('TACTIC_CONFLICT');
  }

  release({ objectId, token, connectionId }) {
    const lease = this.leases.get(objectId);
    if (!lease || lease.connectionId !== connectionId || (token && lease.token !== token)) return false;
    this.leases.delete(objectId);
    return true;
  }

  releaseConnection(connectionId) {
    for (const [objectId, lease] of this.leases) {
      if (lease.connectionId === connectionId) this.leases.delete(objectId);
    }
    for (const key of this.lastPreviewAt.keys()) {
      if (key.startsWith(`${connectionId}:`)) this.lastPreviewAt.delete(key);
    }
  }

  preview({ objectId, token, connectionId, x, y }) {
    this.sweep();
    const lease = this.leases.get(objectId);
    if (!lease || lease.connectionId !== connectionId || lease.token !== token) throw new LeaseError('COLLAB_LOCKED');
    if (![x, y].every((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100)) throw new LeaseError('VALIDATION_ERROR');
    const key = `${connectionId}:${objectId}`;
    const now = this.now();
    if (now - (this.lastPreviewAt.get(key) ?? -Infinity) < 100) return false;
    this.lastPreviewAt.set(key, now);
    return true;
  }

  snapshot() {
    this.sweep();
    return [...this.leases.values()].map(({ objectId, actor, expiresAt }) => ({ objectId, owner: actor.name, color: actor.color, expiresAt }));
  }
}
