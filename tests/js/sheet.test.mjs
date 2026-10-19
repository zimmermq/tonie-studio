// The print page: finding stickers by name or UID, ticking what is shown, and
// sending one field per ticked row.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

function sticker(model, search, count = 0) {
  const pick = element('', '', { dataset: { model } });
  pick.value = `${count}:fill:${model}`;
  return element('', '', {
    dataset: { search, src: '' },
    children: { '.want': element(''), '.times': element(''), '.count': element(''),
                'input[name=picks]': pick },
  });
}

async function page(rows, layouts = []) {
  const find = element('find');
  find.value = '';
  const elements = await runPage(['app.js', 'sheet.js'], {
    ids: ['total', 'onlyticked', 'shown', 'nohits', 'tickshown', 'untick'],
    prepared: { find },
    queries: { '.sticker': rows, 'input[name=layout]': layouts },
    pageData: { layouts: { sheet: { perPage: 20, diameterMm: 46 },
                           punch: { perPage: 15, diameterMm: 44.5 } }, mostCopies: 99,
                words: { 'sheet.total': '{count} covers', 'sheet.sheets': '{count} sheets',
                         'sheet.nothing_chosen': 'none', 'sheet.pictures': '',
                         'list.shown_of': '{shown} of {count}' } },
  });
  const search = text => { find.value = text; find.fire('input'); };
  const shown = () => rows.filter(row => !row.hidden).map(row => row.dataset.search.split(' ')[0]);
  const pick = row => row.querySelector('input[name=picks]');
  return { elements, search, shown, pick };
}

const ROWS = () => [
  sticker('TS-1', 'simba disney e0:04:03:50:1f:dd:f0:44 e00403501fddf044'),
  sticker('TS-2', 'olaf disney e0:04:03:50:0e:ee:34:61 e00403500eee3461'),
  sticker('TS-3', 'wolf 100% e0:04:03:50:17:c5:df:11 e004035017c5df11'),
];

// A UID is found whole, with or without colons, or by its last digits.
{
  const rows = ROWS();
  const { search, shown } = await page(rows);
  search('E0:04:03:50:1F:DD:F0:44');
  assert.deepEqual(shown(), ['simba']);
  search('17c5df11');
  assert.deepEqual(shown(), ['wolf']);
}

// Several UIDs at once, by spaces or commas, find each of them.
{
  const { search, shown } = await page(ROWS());
  search('1fddf044 17c5df11');
  assert.deepEqual(shown(), ['simba', 'wolf']);
  search('olaf, wolf');
  assert.deepEqual(shown(), ['olaf', 'wolf']);
}

// Words of a name must all fit.
{
  const { search, shown, elements } = await page(ROWS());
  search('disney olaf');
  assert.deepEqual(shown(), ['olaf']);
  assert.equal(elements.shown.textContent, '1 of 3');
}

// "Tick all shown" ticks only what the search shows, and those rows are sent.
{
  const rows = ROWS();
  const { search, elements, pick } = await page(rows);
  search('disney');
  elements.tickshown.fire('click');
  assert.deepEqual(rows.map(row => pick(row).value), ['1:fill:TS-1', '1:fill:TS-2', '0:fill:TS-3']);
  assert.deepEqual(rows.map(row => pick(row).disabled), [false, false, true]);
  assert.equal(elements.total.textContent, '2 covers, 1 sheets');
}

// Ticks survive another search, so chips can be found one after the other.
{
  const rows = ROWS();
  const { search, elements, pick } = await page(rows);
  search('simba');
  elements.tickshown.fire('click');
  search('wolf');
  elements.tickshown.fire('click');
  search('');
  assert.deepEqual(rows.map(row => +pick(row).value[0]), [1, 0, 1]);
}

// "Only ticked" shows what is on the sheet; "untick all" clears it.
{
  const rows = [sticker('TS-1', 'a', 1), sticker('TS-2', 'b')];
  const { elements, shown, pick } = await page(rows);
  elements.onlyticked.checked = true;
  elements.onlyticked.fire('change');
  assert.deepEqual(shown(), ['a']);
  elements.untick.fire('click');
  assert.equal(pick(rows[0]).value, '0:fill:TS-1');
  assert.equal(elements.nohits.hidden, false);
}

// The count of sheets follows how the covers are cut.
{
  const choice = value => Object.assign(element('', '', {}), { value, checked: value === 'sheet' });
  const layouts = [choice('sheet'), choice('punch')];
  const rows = Array.from({ length: 16 }, (_, n) => sticker(`TS-${n}`, `t${n}`, 1));
  const { elements } = await page(rows, layouts);
  assert.equal(elements.total.textContent, '16 covers, 1 sheets');
  layouts[0].checked = false;
  layouts[1].checked = true;
  layouts[1].fire('change');
  assert.equal(elements.total.textContent, '16 covers, 2 sheets');
}

// The total counts what the PDF holds: a text side per cover while those are
// printed too, and every extra picture once, so the count of sheets is right.
// The text side's preview follows every change of what is ticked.
function steppable(model, name, count = 0) {
  const row = sticker(model, name, count);
  const minus = element('', '', { dataset: { step: '-1' } });
  const plus = element('', '', { dataset: { step: '1' } });
  row.querySelector = selector => ({ '.want': row.want, '.times': element(''),
                                     '.count': element(''), strong: element('', name),
                                     'input[name=picks]': row.pick })[selector] || null;
  row.querySelectorAll = selector => (selector === 'button[data-step]' ? [minus, plus] : []);
  return Object.assign(row, { minus, plus });
}

async function backPage(rows, { back = false, files = 0 } = {}) {
  for (const row of rows) {
    row.want = element('');
    row.pick = element('', '', { dataset: { model: row.dataset.search } });
    row.pick.value = `${row.count}:fill:${row.dataset.search}`;
  }
  const find = Object.assign(element('find'), { value: '' });
  const disc = Object.assign(element('backdisc'), { parentElement: { style: {} }, innerHTML: '' });
  const wantback = Object.assign(element('wantback'), { checked: back });
  const wanttracks = Object.assign(element('wanttracks'), { checked: true });
  const extras = Object.assign(element(''), { files: Array.from({ length: files }, () => ({})) });
  const answers = Object.fromEntries(rows.map(row => [
    `/api/sheet/back?model=${row.dataset.search}&layout=sheet`, { tracks: [], plain: [] }]));
  const elements = await runPage(['app.js', 'sheet.js'], {
    ids: ['total', 'onlyticked', 'shown', 'nohits', 'tickshown', 'untick', 'fillsheet', 'backwho'],
    prepared: { find, backdisc: disc, wantback, wanttracks },
    queries: { '.sticker': rows },
    found: { 'input[name=files]': extras },
    answers,
    pageData: { layouts: { sheet: { perPage: 20, diameterMm: 46 } }, mostCopies: 99, backFade: 0.5,
                words: { 'sheet.total': '{count} covers', 'sheet.sheets': '{count} sheets',
                         'sheet.backs': '{count} backs', 'sheet.pictures': '{count} pictures',
                         'sheet.nothing_chosen': 'none', 'list.shown_of': '{shown} of {count}' } },
  });
  // Lets a redraw of the text side finish.
  const settle = async () => {
    for (let round = 0; round < 10; round++) await new Promise(done => setImmediate(done));
  };
  return { elements, disc, wantback, extras, settle };
}

function row(model, count = 0) {
  return Object.assign(steppable(model, model), { count });
}

{
  const { elements, wantback, extras } = await backPage([row('A', 12), row('B', 1)], { files: 2 });
  assert.equal(elements.total.textContent, '13 covers, 2 pictures, 1 sheets');
  wantback.checked = true;
  wantback.fire('change');
  assert.equal(elements.total.textContent, '13 covers, 13 backs, 2 pictures, 2 sheets');
  extras.files = [];
  extras.fire('change');
  assert.equal(elements.total.textContent, '13 covers, 13 backs, 2 sheets');
}

// Extra pictures alone are something to print as well.
{
  const { elements } = await backPage([row('A')], { files: 3 });
  assert.equal(elements.total.textContent, '3 pictures, 1 sheets');
}

{
  const rows = [row('A'), row('B')];
  const { elements, disc, settle } = await backPage(rows, { back: true });
  assert.equal(elements.backwho.textContent, 'A');
  rows[1].plus.fire('click');
  await settle();
  assert.equal(elements.backwho.textContent, 'B');
  rows[1].minus.fire('click');
  await settle();
  assert.equal(elements.backwho.textContent, 'A');
  rows[1].plus.fire('click');
  await settle();
  elements.untick.fire('click');
  await settle();
  assert.equal(elements.backwho.textContent, 'A');
  disc.innerHTML = 'stale';
  elements.fillsheet.fire('click');
  await settle();
  assert.equal(disc.innerHTML, '');
}
console.log('sheet.test: totals and back ok');
