/* Exercise the actual browser CRDT functions without a browser or dependencies.
 * Run from the project root: node tests/crdt_smoke.cjs
 */
'use strict';

const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const original = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const footer = /\n  bind\(\);\s*const fromUrl = [\s\S]*?\n\}\)\(\);\s*$/;
assert.match(original, footer, 'Expected to expose CRDT functions before the app initializes');
const source = original.replace(
  footer,
  '\n  window.__crdt = { state, nextNode, applyOps, answerText, comparePositions };\n})();'
);

function makeActor() {
  const window = { DECIPHER_API_BASE: '' };
  const document = { getElementById: () => ({}) };
  const location = { origin: 'http://localhost:8000', hostname: 'localhost', protocol: 'http:', href: 'http://localhost:8000/' };
  vm.runInNewContext(source, { window, document, crypto: webcrypto, location });
  return window.__crdt;
}

const alice = makeActor();
const bob = makeActor();
assert.notEqual(alice.state.actor, bob.state.actor, 'Each browser instance needs a distinct actor ID');

let left = null;
let deepest = 0;
for (let index = 0; index < 10_000; index++) {
  const next = alice.nextNode(left, null, String(index % 10));
  if (left) assert.ok(alice.comparePositions(left, next.pos) < 0, 'Append order must be preserved');
  assert.ok(next.pos.length <= 256, 'Position must fit the server limit');
  deepest = Math.max(deepest, next.pos.length);
  left = next.pos;
}

const a = alice.nextNode(null, null, 'A');
const b = bob.nextNode(null, null, 'B');
const fromAlice = { inserts: [a], deletes: [] };
const fromBob = { inserts: [b], deletes: [] };
const one = { answer_nodes: [] };
const two = { answer_nodes: [] };
alice.applyOps(one, fromAlice);
alice.applyOps(one, fromBob);
bob.applyOps(two, fromBob);
bob.applyOps(two, fromAlice);
assert.equal(alice.answerText(one), bob.answerText(two), 'Concurrent inserts must converge');
assert.equal(alice.answerText(one).length, 2, 'Both concurrent characters must survive');
alice.applyOps(one, fromAlice);
assert.equal(one.answer_nodes.length, 2, 'Replayed inserts must be idempotent');

const removal = { inserts: [], deletes: [a.id] };
alice.applyOps(one, removal);
alice.applyOps(one, removal);
bob.applyOps(two, removal);
assert.equal(alice.answerText(one), bob.answerText(two), 'Deletes must converge');
const outOfOrder = { answer_nodes: [] };
alice.applyOps(outOfOrder, { inserts: [], deletes: [b.id] });
alice.applyOps(outOfOrder, fromBob);
assert.equal(alice.answerText(outOfOrder), '', 'Delete before insert must remain deleted');

// Seeded random locations exercise gaps and prefixes, including adjacent nodes.
let seed = 0x845c2b5;
function rand(max) {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed % max;
}
const positions = [];
for (let index = 0; index < 5_000; index++) {
  const location = rand(positions.length + 1);
  const before = positions[location - 1] ?? null;
  const after = positions[location] ?? null;
  const next = bob.nextNode(before, after, 'x').pos;
  assert.ok(!before || bob.comparePositions(before, next) < 0, 'Insert must follow left neighbor');
  assert.ok(!after || bob.comparePositions(next, after) < 0, 'Insert must precede right neighbor');
  assert.ok(next.length <= 256, 'Random insertion must fit the server limit');
  deepest = Math.max(deepest, next.length);
  positions.splice(location, 0, next);
}

console.log(`CRDT smoke passed: 10,000 appends, 5,000 gap inserts, two concurrent actors, replay, deletion. Max position depth: ${deepest}.`);
