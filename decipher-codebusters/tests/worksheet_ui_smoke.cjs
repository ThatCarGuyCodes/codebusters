/* Verify the actual worksheet renderer in a Node VM without a browser.
 * Run from the project root: node tests/worksheet_ui_smoke.cjs
 * The DOM shim captures the HTML emitted by renderCipher, then checks
 * editable cell counts/keys for the layouts in the attached PDF screenshots.
 */
'use strict';

const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const original = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const footer = /\n  bind\(\);\s*const fromUrl = [\s\S]*?\n\}\)\(\);\s*$/;
assert.match(original, footer, 'Expected to expose renderer before the app initializes');
const source = original.replace(footer,
  '\n  window.__worksheet = { state, renderCipher, onMessage, handleCellInput, handleCellBlur };\n})();');

function shimElement(id, document) {
  let markup = '';
  let fields = [];
  const element = {
    id, textContent: '', hidden: false, style: {}, value: '',
    classList: { toggle() {}, add() {}, remove() {} },
    contains(child) { return fields.includes(child); },
    querySelector() { return null; },
    querySelectorAll(selector) {
      return selector === '.work-cell-letter'
        ? fields.filter(field => field.classList.contains('work-cell-letter')) : fields;
    },
    replaceChildren() { this.innerHTML = ''; },
    addEventListener() {}, removeAttribute() {},
  };
  Object.defineProperty(element, 'innerHTML', {
    get() { return markup; },
    set(value) {
      markup = String(value);
      fields = (markup.match(/<input\b[^>]*data-cell-key="[^"]+"[^>]*>/gi) || []).map(tag => {
        const key = tag.match(/\bdata-cell-key="([^"]+)"/)[1];
        const initial = tag.match(/\bvalue="([^"]*)"/);
        const className = tag.match(/\bclass="([^"]*)"/);
        return {
          dataset: { cellKey: key }, value: initial ? initial[1] : '',
          selectionStart: 0, selectionEnd: 0,
          classList: { contains(name) { return className?.[1].split(/\s+/).includes(name) || false; } },
          closest(selector) { return selector === '[data-cell-key]' ? this : null; },
          focus() { document.activeElement = this; },
          select() {}, setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
        };
      });
    },
  });
  return element;
}

function browser() {
  const elements = new Map();
  const document = {
    activeElement: null,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, shimElement(id, document));
      return elements.get(id);
    },
    querySelector() { return null; },
    createElement(tag) { return shimElement(tag, document); },
  };
  const window = { DECIPHER_API_BASE: '' };
  const location = {
    origin: 'http://localhost:8000', hostname: 'localhost', protocol: 'http:',
    href: 'http://localhost:8000/', pathname: '/', search: '',
  };
  vm.runInNewContext(source, { window, document, location, crypto: webcrypto, URL, URLSearchParams });
  return { ui: window.__worksheet, elements, document };
}

function worksheetHTML(ui, elements, q) {
  ui.state.room = { id: 'room', questions: [q] };
  ui.state.activeId = q.id;
  ui.renderCipher(q);
  return elements.get('cipher-workspace').innerHTML;
}

function inputs(html) {
  const tags = html.match(/<input\b[^>]*>/gi) || [];
  return new Map(tags.map(tag => {
    const key = tag.match(/\bdata-cell-key="([^"]+)"/);
    return key ? [key[1], tag] : [];
  }).filter(entry => entry.length));
}

const { ui, elements, document } = browser();

const crypt = {
  id: 'crypt', points: 313, cipher_kind: 'cryptarithm', work_cells: {},
  cipher_layout: { lines: ['O T I S', '+ B Y R O N', 'S P R O U T', 'ABC DEF'], tables: [{
    type: 'cryptarithm', decode_digits: '2734851', formula_rows: ['OTIS', '+BYRON', 'SPROUT'],
    digits: Array.from({ length: 10 }, (_, index) => String(index + 1)), letters: 'ABCDEFGHIJ'.split(''),
  }] },
};
let html = worksheetHTML(ui, elements, crypt);
let boxes = inputs(html);
assert.equal(boxes.size, 128, 'Cryptarithm needs 7 decoded + 100 grid + 15 formula + 6 extra cipher cells');
for (let index = 0; index < 7; index++) assert.ok(boxes.has(`crypt:decode:${index}`));
for (let row = 0; row < 10; row++) for (let col = 0; col < 10; col++)
  assert.ok(boxes.has(`crypt:grid:${row}:${col}`), `Missing digit grid cell ${row},${col}`);
for (const [row, letters] of ['OTIS', '+BYRON', 'SPROUT'].entries())
  for (const [column, character] of Array.from(letters).entries())
    if (/^[A-Z]$/.test(character))
      assert.ok(boxes.has(`crypt:formula:${row}:${column}`), `Missing formula input ${row},${column}`);
assert.ok(!boxes.has('crypt:formula:1:0'), 'The plus sign is a fixed arithmetic operator');
for (const index of [0, 1, 2, 4, 5, 6])
  assert.ok(boxes.has(`line:0:char:${index}`), 'Other printed cryptarithm lines need per-character typing');
assert.match(html, /Cryptarithm formula/i);
const gridHeader = html.match(/<table[^>]*\bcrypt-grid\b[^>]*>[\s\S]*?<thead><tr>([\s\S]*?)<\/tr>/)?.[1] || '';
assert.deepEqual([...gridHeader.matchAll(/<th\b[^>]*>([^<]*)<\/th>/g)].map(match => match[1]).filter(Boolean),
  Array.from({ length: 10 }, (_, index) => String(index + 1)), 'Cryptarithm columns must show 1–10');
const cryptRows = html.match(/<table[^>]*\bcrypt-grid\b[^>]*>[\s\S]*?<tbody>([\s\S]*?)<\/tbody>/)?.[1] || '';
assert.deepEqual([...cryptRows.matchAll(/<th\s+scope="row"[^>]*>([^<]*)<\/th>/g)].map(match => match[1]),
  'ABCDEFGHIJ'.split(''), 'Cryptarithm rows must show A–J');
const separatedPlus = {
  ...crypt, id: 'crypt-detached-plus', work_cells: {},
  cipher_layout: { ...crypt.cipher_layout,
    lines: ['O T I S', '+', 'B Y R O N', 'S P R O U T', 'ABC DEF'] },
};
const separatedHtml = worksheetHTML(ui, elements, separatedPlus);
const separatedBoxes = inputs(separatedHtml);
assert.equal((separatedHtml.match(/class="cipher-character-row"/g) || []).length, 1,
  'A detached plus must not leave a duplicate BYRON ciphertext line');
assert.equal([...separatedBoxes.keys()].filter(key => key.startsWith('line:')).length, 6,
  'Only the unrelated ABC DEF line needs normal per-character boxes');
for (let index = 1; index <= 5; index++)
  assert.ok(separatedBoxes.has(`crypt:formula:1:${index}`), `BYRON formula letter ${index} needs typing`);

// A partner's input must update state and appear in the regenerated worksheet.
worksheetHTML(ui, elements, crypt);
ui.onMessage({ type: 'cell_update', question_id: crypt.id, key: 'crypt:grid:3:4', value: '7' });
assert.equal(crypt.work_cells['crypt:grid:3:4'], '7');
let input = elements.get('cipher-workspace').querySelectorAll('[data-cell-key]')
  .find(field => field.dataset.cellKey === 'crypt:grid:3:4');
assert.equal(input.value, '7', 'Partner update should immediately appear in visible box');
input.focus();
ui.onMessage({ type: 'cell_update', question_id: crypt.id, key: 'crypt:grid:3:4', value: '8' });
assert.equal(input.value, '7', 'Incoming change should preserve the focused editor');
assert.equal(crypt.work_cells['crypt:grid:3:4'], '8');
document.activeElement = null;
ui.handleCellBlur({ target: input });
assert.equal(input.value, '8', 'The latest shared value appears when focus leaves the cell');

const morseLines = ['LIGNAXHPCSYKQNCP', 'ETFARXBCEBF', 'YVLFARAAVVICIJ', 'YERQRCURAYBD', 'LGQLLIHA'];
const morse = {
  id: 'morse', points: 303, cipher_kind: 'fractionated_morse', work_cells: {},
  cipher_layout: { lines: morseLines, tables: [{
    type: 'morse_fraction', columns: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''),
    fractions: Array.from({ length: 26 }, (_, index) =>
      [9, 3, 1].map(divisor => '.-x'[Math.floor(index / divisor) % 3]).join('')),
    replacement: Array(26).fill(''),
  }] },
};
html = worksheetHTML(ui, elements, morse);
boxes = inputs(html);
for (let index = 0; index < 26; index++)
  assert.ok(boxes.has(`table:0:row:0:col:${index}`), `Missing Morse replacement box ${index}`);
for (let row = 0; row < morseLines.length; row++) {
  for (let col = 0; col < morseLines[row].length; col++)
    assert.ok(boxes.has(`line:${row}:char:${col}`), `Missing ciphertext writing cell ${row},${col}`);
}
assert.equal([...boxes.keys()].filter(key => key.startsWith('line:')).length,
  morseLines.join('').length, 'Every cipher letter should have its own aligned input');
const expectedTriples = Array.from({ length: 26 }, (_, index) =>
  [9, 3, 1].map(divisor => '•−×'[Math.floor(index / divisor) % 3]).join(''));
assert.deepEqual([...html.matchAll(/class="morse-triple" aria-label="([^"]+)"/g)].map(match => match[1]),
  expectedTriples, 'Morse chart must print all 26 correct fractions in the original sequence');
const firstLetter = elements.get('cipher-workspace').querySelectorAll('.work-cell-letter')[0];
firstLetter.value = 'T';
ui.handleCellInput({ target: firstLetter, isComposing: false });
assert.equal(morse.work_cells['line:0:char:0'], 'T', 'Typing one letter updates only its own shared cell');
assert.equal(document.activeElement?.dataset.cellKey, 'line:0:char:1', 'Typing moves focus to the next letter');
const partner = browser();
const partnerMorse = JSON.parse(JSON.stringify(morse));
partnerMorse.work_cells = {};
worksheetHTML(partner.ui, partner.elements, partnerMorse);
const secondLetter = partner.elements.get('cipher-workspace').querySelectorAll('.work-cell-letter')[1];
secondLetter.value = 'E';
partner.ui.handleCellInput({ target: secondLetter, isComposing: false });
ui.onMessage({ type: 'cell_update', question_id: morse.id, key: 'line:0:char:1', value: 'E' });
partner.ui.onMessage({ type: 'cell_update', question_id: morse.id, key: 'line:0:char:0', value: 'T' });
assert.deepEqual([morse.work_cells['line:0:char:0'], morse.work_cells['line:0:char:1']], ['T', 'E']);
assert.deepEqual([partnerMorse.work_cells['line:0:char:0'], partnerMorse.work_cells['line:0:char:1']], ['T', 'E'],
  'Two browsers editing separate cipher letters must converge without replacing each other');

const frequency = {
  id: 'frequency', cipher_kind: 'aristocrat', work_cells: {},
  cipher_layout: { lines: ['YMJ VZNHP GWTBS KTC'], tables: [{
    type: 'frequency', headers: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''),
    rows: [{ label: 'Frequency', values: Array(26).fill('1') },
      { label: 'Replacement', values: Array(26).fill('') }],
  }] },
};
html = worksheetHTML(ui, elements, frequency);
boxes = inputs(html);
for (let index = 0; index < 26; index++)
  assert.ok(boxes.has(`table:0:row:2:col:${index}`), `Missing frequency replacement ${index}`);
assert.ok(boxes.has('line:0:char:0'), 'Ciphertext needs aligned typing cells');

function worksheetRows(markup) {
  return [...markup.matchAll(/<th\s+scope="row"[^>]*>([^<]+)<\/th>/g)].map(match => match[1]);
}
const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
const k2 = {
  id: 'k2', cipher_kind: 'aristocrat', work_cells: {},
  cipher_layout: { lines: ['ABBA ZZYY'], tables: [{
    type: 'frequency', header_label: 'K2', headers: letters,
    rows: [{ label: 'Replacement', values: Array(26).fill('') },
      { label: 'K2', values: letters },
      { label: 'Frequency', values: letters.map(letter => 'ABBAZZYY'.split(letter).length - 1 + '') }],
  }] },
};
html = worksheetHTML(ui, elements, k2);
boxes = inputs(html);
assert.deepEqual(worksheetRows(html), ['Replacement', 'K2', 'Frequency'], 'K2 order must show replacement first');
for (let index = 0; index < 26; index++)
  assert.ok(boxes.has(`table:0:row:0:col:${index}`), `Missing K2 replacement ${index}`);
assert.equal([...boxes.keys()].filter(key => key.startsWith('table:')).length, 26, 'Alphabet and counts are fixed');
for (const label of ['K1', 'K3', 'Alphabet']) {
  const normal = {
    id: `normal-${label}`, cipher_kind: 'patristocrat', work_cells: {},
    cipher_layout: { lines: ['FGHI JKLM NOPQ'], tables: [{
      type: 'frequency', header_label: label, headers: letters,
      rows: [{ label, values: letters }, { label: 'Frequency', values: Array(26).fill('1') },
        { label: 'Replacement', values: Array(26).fill('') }],
    }] },
  };
  html = worksheetHTML(ui, elements, normal);
  boxes = inputs(html);
  assert.deepEqual(worksheetRows(html), [label, 'Frequency', 'Replacement'], `Wrong ${label} table row order`);
  for (let index = 0; index < 26; index++)
    assert.ok(boxes.has(`table:0:row:2:col:${index}`), `Missing ${label} replacement ${index}`);
}
const spanish = 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZ'.split('');
const xeno = {
  id: 'xenocrypt', cipher_kind: 'xenocrypt', work_cells: {},
  cipher_layout: { lines: ['NIÑA CAMINO PIÑATA'], tables: [{
    type: 'frequency', header_label: 'K2', headers: spanish,
    rows: [{ label: 'Replacement', values: Array(27).fill('') },
      { label: 'K2', values: spanish },
      { label: 'Frequency', values: spanish.map(letter => String('NIÑACAMINOPIÑATA'.split(letter).length - 1)) }],
  }] },
};
html = worksheetHTML(ui, elements, xeno);
boxes = inputs(html);
assert.deepEqual(worksheetRows(html), ['Replacement', 'K2', 'Frequency']);
assert.match(html, /<th\b[^>]*>Ñ<\/th>|<span[^>]*>Ñ<\/span>/, 'Xenocrypt shows the Ñ column');
for (let index = 0; index < 27; index++)
  assert.ok(boxes.has(`table:0:row:0:col:${index}`), `Missing Xenocrypt replacement ${index}`);

const baconian = {
  id: 'baconian', cipher_kind: 'baconian', work_cells: {},
  cipher_layout: { lines: ['AABAA ABBBA BAAAA'], tables: [{
    type: 'baconian', headers: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''),
    rows: [{ label: 'Code', values: Array(26).fill('AABBA') },
      { label: 'Replacement', values: Array(26).fill('') }],
  }] },
};
boxes = inputs(worksheetHTML(ui, elements, baconian));
for (let index = 0; index < 26; index++)
  assert.ok(boxes.has(`table:0:row:2:col:${index}`), `Missing Baconian replacement ${index}`);
const baconianRows = {
  ...baconian, id: 'baconian-three-rows', cipher_layout: { lines: ['AABAA ABBBA BAAAA'], tables: [{
    type: 'baconian', headers: letters,
    rows: [{ label: 'Alphabet', values: letters },
      { label: 'Baconian', values: letters.map(() => 'AABBA') },
      { label: 'Replacement', values: Array(26).fill('') }],
  }] },
};
html = worksheetHTML(ui, elements, baconianRows);
assert.deepEqual(worksheetRows(html), ['Alphabet', 'Baconian', 'Replacement']);
boxes = inputs(html);
for (let index = 0; index < 26; index++)
  assert.ok(boxes.has(`table:0:row:2:col:${index}`), `Missing Baconian bottom replacement ${index}`);

const square = {
  id: 'nihilist', cipher_kind: 'nihilist', work_cells: {},
  cipher_layout: { lines: ['23 54 12 35'], tables: [{
    type: 'keyed_square', column_headers: '12345'.split(''), row_headers: '12345'.split(''),
    values: Array.from({ length: 5 }, () => Array(5).fill('')),
  }] },
};
boxes = inputs(worksheetHTML(ui, elements, square));
for (let row = 0; row < 5; row++) for (let col = 0; col < 5; col++)
  assert.ok(boxes.has(`table:0:row:${row}:col:${col}`), `Missing Nihilist square ${row},${col}`);
assert.equal([...boxes.keys()].filter(key => key.startsWith('table:')).length, 25, 'Nihilist header edges stay fixed');
const board = {
  id: 'checkerboard', cipher_kind: 'checkerboard', work_cells: {},
  cipher_layout: { lines: ['AABCD AABCD'], tables: [{
    type: 'checkerboard', values: Array.from({ length: 6 }, () => Array(6).fill('')),
  }] },
};
html = worksheetHTML(ui, elements, board);
boxes = inputs(html);
for (let row = 0; row < 6; row++) for (let col = 0; col < 6; col++)
  assert.ok(boxes.has(`table:0:row:${row}:col:${col}`), `Missing Checkerboard box ${row},${col}`);
assert.equal([...boxes.keys()].filter(key => key.startsWith('table:')).length, 36, 'Checkerboard has 36 blank boxes');

const generic = { id: 'generic', cipher_kind: 'generic', work_cells: {},
  cipher_layout: { lines: ['ABC DEF'], tables: [] } };
boxes = inputs(worksheetHTML(ui, elements, generic));
assert.deepEqual([...boxes.keys()], [0, 1, 2, 4, 5, 6].map(index => `line:0:char:${index}`),
  'Generic cipher should offer a writing box beneath each printed character');

// Older rooms may have no structured table at all; the worksheet must still
// generate its printable table from cipher type and the saved ciphertext.
for (const [kind, prompt, expectedKey] of [
  ['aristocrat', 'Solve this Aristocrat K2 cipher.', 'table:0:row:0:col:25'],
  ['patristocrat', 'Solve this Patristocrat K1 cipher.', 'table:0:row:2:col:25'],
  ['xenocrypt', 'Solve this Xenocrypt cipher.', 'table:0:row:2:col:26'],
  ['baconian', 'Solve this Baconian cipher.', 'table:0:row:2:col:25'],
  ['nihilist', 'Solve this Nihilist cipher.', 'table:0:row:4:col:4'],
  ['checkerboard', 'Solve this Checkerboard cipher.', 'table:0:row:5:col:5'],
  ['fractionated_morse', 'Solve this Fractionated Morse cipher.', 'table:0:row:0:col:25'],
  ['cryptarithm', 'What do the values 315257 decode to? Cryptarithm cipher.', 'crypt:grid:9:9'],
]) {
  const oldRoomQuestion = { id: `old-${kind}`, cipher_kind: kind, prompt,
    cipher: 'ABBA ZZYY', work_cells: {}, cipher_layout: {} };
  const oldHtml = worksheetHTML(ui, elements, oldRoomQuestion);
  const oldBoxes = inputs(oldHtml);
  assert.ok(oldBoxes.has(expectedKey), `Missing generated ${kind} table when PDF has no table text`);
  assert.ok(oldBoxes.has('line:0:char:0'), `Missing writing boxes for old ${kind} cipher`);
  if (kind === 'xenocrypt') assert.match(oldHtml, /<th\b[^>]*>Ñ<\/th>/);
  if (kind === 'aristocrat') assert.deepEqual(worksheetRows(oldHtml), ['Replacement', 'K2', 'Frequency']);
}

console.log('Worksheet UI smoke passed: K2/K1/K3/Xenocrypt/Baconian, 6×6 Checkerboard, Nihilist, Morse, cryptarithm formula and grid, per-character collaboration.');
