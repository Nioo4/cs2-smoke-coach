import * as Y from 'yjs';
import { projectFormalDocument } from '../src/formal-document.mjs';

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const cases = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const results = cases.map((projection) => {
  const doc = new Y.Doc();
  try {
    doc.getMap('document').set('structure', projection.structure);
    for (const [key, value] of Object.entries(projection.fields)) { const text = new Y.Text(); text.insert(0, value); doc.getMap('fields').set(key, text); }
    projectFormalDocument(doc); return true;
  } catch (error) {
    const oversized = Object.entries(projection.fields).filter(([key, value]) => [...value].length > (key === 'tactic.title' ? 100 : 2000)).map(([key, value]) => ({ key, length: [...value].length }));
    process.stderr.write(`${error.message} ${JSON.stringify(oversized)}\n`); return false;
  } finally { doc.destroy(); }
});
process.stdout.write(JSON.stringify(results));
