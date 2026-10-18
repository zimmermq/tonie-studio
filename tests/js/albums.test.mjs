// The album list: ticking several albums at once for a batch, over what the
// search and the chips leave in view. Search, chips and order are list.js's.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

function row() {
  const box = element('');
  return element('', '', { dataset: { search: 'x' }, children: { 'input[name=ids]': box } });
}

async function page(rows) {
  const elements = await runPage(['app.js', 'index.js'], {
    ids: ['loading', 'pick', 'pickcount', 'albums', 'upload', 'picked', 'send', 'tickshown',
          'untick'],
    pageData: { words: { 'batch.picked': { one: '{count} album', other: '{count} albums' },
                         'albums.pick_slow': 'slow' },
                slowPick: 3 },
    queries: { '#albums .pickrow': rows },
  });
  const ticked = () => rows.map(r => r.querySelector('input[name=ids]').checked);
  return { elements, ticked };
}

// "Tick all shown" ticks what is in view and leaves the rest as it was.
{
  const rows = [row(), row(), row()];
  rows[1].hidden = true;
  const { elements, ticked } = await page(rows);
  elements.tickshown.fire('click');
  assert.deepEqual(ticked(), [true, false, true]);
  assert.equal(elements.pick.hidden, false);
  assert.equal(elements.pickcount.textContent, '2 albums');
}

// A tick stays when the view changes, so two searches can be combined.
{
  const rows = [row(), row()];
  const { elements, ticked } = await page(rows);
  rows[1].hidden = true;
  elements.tickshown.fire('click');
  rows[1].hidden = false;
  rows[0].hidden = true;
  elements.tickshown.fire('click');
  assert.deepEqual(ticked(), [true, true]);
}

// "Untick all" clears every row and hides the bar.
{
  const rows = [row(), row()];
  const { elements, ticked } = await page(rows);
  elements.tickshown.fire('click');
  elements.untick.fire('click');
  assert.deepEqual(ticked(), [false, false]);
  assert.equal(elements.pick.hidden, true);
}

// Shift-click ticks the range from the last click, skipping hidden rows.
{
  const rows = [row(), row(), row(), row(), row()];
  rows[2].hidden = true;
  const { ticked } = await page(rows);
  const box = index => rows[index].querySelector('input[name=ids]');
  box(0).checked = true;
  box(0).fire('click', {});
  box(4).checked = true;
  box(4).fire('click', { shiftKey: true });
  assert.deepEqual(ticked(), [true, true, false, true, true]);
}

// From the slow threshold on, the bar says that setting up takes a while.
{
  const rows = [row(), row(), row()];
  const { elements } = await page(rows);
  elements.tickshown.fire('click');
  assert.match(elements.pickcount.textContent, /slow/);
}
console.log('albums.test: ok');
