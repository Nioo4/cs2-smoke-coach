import { Pool } from 'pg';
import { createInitialData, createYDoc, isPrototypeDocumentId } from './prototype-schema.mjs';
import { createStageInitialData, createStageYDoc, isStageDocumentId } from './stage-board-schema.mjs';
import * as Y from 'yjs';

export class PostgresStore {
  constructor(connectionString, {
    connectionTimeoutMillis = 3_000,
    queryTimeoutMillis = 5_000,
    statementTimeoutMillis = 5_000,
    idleInTransactionSessionTimeoutMillis = 5_000,
  } = {}) {
    this.poolErrorCount = 0;
    this.lastPoolError = null;
    this.pool = new Pool({
      connectionString,
      max: 5,
      application_name: 'smokeline-tactics-t0',
      connectionTimeoutMillis,
      query_timeout: queryTimeoutMillis,
      statement_timeout: statementTimeoutMillis,
      idle_in_transaction_session_timeout: idleInTransactionSessionTimeoutMillis,
    });
    this.pool.on('error', (error) => {
      this.poolErrorCount += 1;
      this.lastPoolError = {
        code: typeof error?.code === 'string' ? error.code : 'unknown',
        at: Date.now(),
      };
    });
  }

  async initialize() {
    await this.pool.query(`
      CREATE TABLE IF NOT EXISTS tactics_t0_documents (
        document_id text PRIMARY KEY,
        epoch uuid NOT NULL,
        revision bigint NOT NULL CHECK (revision >= 0),
        yjs_state bytea NOT NULL,
        structure jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE IF NOT EXISTS tactics_t0_operations (
        document_id text NOT NULL REFERENCES tactics_t0_documents(document_id) ON DELETE CASCADE,
        operation_id uuid NOT NULL,
        epoch uuid NOT NULL,
        actor_id text NOT NULL,
        fingerprint char(64) NOT NULL,
        operation_type text NOT NULL,
        revision bigint NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (document_id, operation_id)
      );
    `);
  }

  async loadOrCreate(documentId) {
    const stageDocument = isStageDocumentId(documentId);
    if (!stageDocument && !isPrototypeDocumentId(documentId)) throw new Error('unknown_document');
    const seed = stageDocument ? createStageInitialData(documentId) : createInitialData(documentId);
    const seedDoc = stageDocument ? createStageYDoc(seed.fields) : createYDoc(seed.fields);
    const state = Buffer.from(Y.encodeStateAsUpdate(seedDoc));
    seedDoc.destroy();
    await this.pool.query(
      `INSERT INTO tactics_t0_documents(document_id, epoch, revision, yjs_state, structure)
       VALUES ($1, $2, 0, $3, $4::jsonb) ON CONFLICT (document_id) DO NOTHING`,
      [documentId, seed.epoch, state, JSON.stringify(seed.structure)],
    );
    const result = await this.pool.query(
      'SELECT document_id, epoch, revision, yjs_state, structure FROM tactics_t0_documents WHERE document_id = $1',
      [documentId],
    );
    if (!result.rowCount) throw new Error('document_not_found');
    return rowDocument(result.rows[0]);
  }

  async findOperation(documentId, operationId) {
    const result = await this.pool.query(
      `SELECT epoch, actor_id, fingerprint, operation_type, revision
       FROM tactics_t0_operations WHERE document_id = $1 AND operation_id = $2`,
      [documentId, operationId],
    );
    if (!result.rowCount) return null;
    const row = result.rows[0];
    return { epoch: row.epoch, actorId: row.actor_id, fingerprint: row.fingerprint.trim(), type: row.operation_type, revision: Number(row.revision) };
  }

  async persist({ documentId, epoch, expectedRevision, operationId, actorId, fingerprint, type, state, structure }) {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const locked = await client.query(
        'SELECT epoch, revision FROM tactics_t0_documents WHERE document_id = $1 FOR UPDATE',
        [documentId],
      );
      if (!locked.rowCount) throw new Error('document_not_found');
      const current = locked.rows[0];
      if (current.epoch !== epoch) throw new Error('stale_epoch');
      const existing = await client.query(
        `SELECT epoch, actor_id, fingerprint, operation_type, revision
         FROM tactics_t0_operations WHERE document_id = $1 AND operation_id = $2`,
        [documentId, operationId],
      );
      if (existing.rowCount) {
        const row = existing.rows[0];
        if (row.epoch !== epoch || row.actor_id !== actorId || row.fingerprint.trim() !== fingerprint) throw new Error('operation_id_reused');
        await client.query('COMMIT');
        return { operationId, epoch, revision: Number(row.revision), duplicate: true };
      }
      if (Number(current.revision) !== expectedRevision) throw new Error('revision_conflict');
      const revision = expectedRevision + 1;
      await client.query(
        `UPDATE tactics_t0_documents SET revision = $2, yjs_state = $3, structure = $4::jsonb, updated_at = now()
         WHERE document_id = $1`,
        [documentId, revision, state, JSON.stringify(structure)],
      );
      await client.query(
        `INSERT INTO tactics_t0_operations(document_id, operation_id, epoch, actor_id, fingerprint, operation_type, revision)
         VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [documentId, operationId, epoch, actorId, fingerprint, type, revision],
      );
      await client.query('COMMIT');
      return { operationId, epoch, revision, duplicate: false };
    } catch (error) {
      await client.query('ROLLBACK').catch(() => {});
      throw error;
    } finally {
      client.release();
    }
  }

  async close() {
    await this.pool.end();
  }
}

function rowDocument(row) {
  return {
    documentId: row.document_id,
    epoch: row.epoch,
    revision: Number(row.revision),
    state: Buffer.from(row.yjs_state),
    structure: row.structure,
  };
}
