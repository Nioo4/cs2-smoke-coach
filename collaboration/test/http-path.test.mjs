import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decodeRequestPath } from '../src/http-path.mjs';

test('malformed percent escapes are rejected without throwing at the request boundary', () => {
  assert.equal(decodeRequestPath('/assets/app.js'), '/assets/app.js');
  assert.equal(decodeRequestPath('/%E0%A4%A'), null);
});
