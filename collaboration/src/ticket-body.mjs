import { DEFAULT_DOCUMENT_ID } from './prototype-schema.mjs';
import { STAGE_DOCUMENT_ID } from './stage-board-schema.mjs';

export const MAX_TICKET_BODY_BYTES = 1024;

export async function readTicketDocumentId(request) {
  const lengthHeader = request.headers?.['content-length'];
  if (lengthHeader !== undefined) {
    if (!/^\d+$/.test(String(lengthHeader))) throw new Error('invalid_ticket_request');
    if (Number(lengthHeader) > MAX_TICKET_BODY_BYTES) throw new Error('ticket_request_too_large');
  }
  const chunks = [];
  let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > MAX_TICKET_BODY_BYTES) throw new Error('ticket_request_too_large');
    chunks.push(chunk);
  }
  if (!bytes) return DEFAULT_DOCUMENT_ID;
  const contentType = String(request.headers?.['content-type'] ?? '').split(';', 1)[0].trim().toLowerCase();
  if (contentType !== 'application/json') throw new Error('invalid_ticket_request');
  let body;
  try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Error('invalid_ticket_request'); }
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length !== 1 || Object.keys(body)[0] !== 'document_id') throw new Error('invalid_ticket_request');
  if (![DEFAULT_DOCUMENT_ID, STAGE_DOCUMENT_ID].includes(body.document_id)) throw new Error('unknown_document');
  return body.document_id;
}
