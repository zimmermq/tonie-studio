// The album list: selecting several albums at once, over what the search shows.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

function row(tonie, isNew = false) {
  const box = element('');
  return element('', '', {
    dataset: { search: 'x', tonie: tonie ? 'yes' : 'no', new: isNew ? 'yes' : 'no' },
    children: { 'input[name=ids]': box },
  });
}
const button = pick => element('', '', { dataset: { pick } });

async function page(rows) {
  const buttons = ['all', 'without', 'new', 'none'].map(button);
  const elements = await runPage(['app.js', 'index.js'], {
    ids: ['search', 'count', 'nohits', 'loading', 'pick', 'pickcount', 'albums', 'upload',
          'picked', 'send'],
    pageData: { words: { 'batch.picked': { one: '{count} album', other: '{count} albums' },
                         'albums.pick_slow': 'slow', 'albums.count': '', 'albums.shown_of': '' },
                slowPick: 3 },
    queries: { '#albums .pickrow': rows, '[data-pick]': buttons },
  });
  const press = pick => buttons.find(b => b.dataset.pick === pick).fire('click');
  const ticked = () => rows.map(r => r.querySelector('input[name=ids]').checked);
  return { elements, buttons, press, ticked };
}

// "Without tonie" leaves the albums that have one alone.
{
  const rows = [row(false), row(true), row(false)];
  const { press, ticked, elements } = await page(rows);
  press('without');
  assert.deepEqual(ticked(), [true, false, true]);
  assert.equal(elements.pick.hidden, false);
  assert.equal(elements.pickcount.textContent, '2 albums');
}

// Only what the search left visible is picked.
{
  const rows = [row(false), row(false), row(false)];
  rows[1].hidden = true;
  const { press, ticked } = await page(rows);
  press('all');
  assert.deepEqual(ticked(), [true, false, true]);
}

// A pick sets the selection of the visible rows rather than adding to it:
// "all" and then "without a tonie" leaves only the albums without one.
{
  const rows = [row(false), row(true), row(false, true)];
  const { press, ticked, elements } = await page(rows);
  press('all');
  press('without');
  assert.deepEqual(ticked(), [true, false, true]);
  assert.equal(elements.pickcount.textContent, '2 albums');
  press('new');
  assert.deepEqual(ticked(), [false, false, true]);
}

// Rows the search hides keep their tick, so two searches can be combined.
{
  const rows = [row(true), row(false)];
  const { press, ticked } = await page(rows);
  rows[0].querySelector('input[name=ids]').checked = true;
  rows[0].hidden = true;
  press('without');
  assert.deepEqual(ticked(), [true, true]);
}

// "New" picks the new ones; "none" clears everything.
{
  const rows = [row(false, true), row(false), row(true, true)];
  const { press, ticked, elements } = await page(rows);
  press('new');
  assert.deepEqual(ticked(), [true, false, true]);
  press('none');
  assert.deepEqual(ticked(), [false, false, false]);
  assert.equal(elements.pick.hidden, true);
}

// A button with nothing to pick is greyed out instead of doing nothing.
{
  const { buttons } = await page([row(true), row(true)]);
  assert.equal(buttons.find(b => b.dataset.pick === 'without').disabled, true);
  assert.equal(buttons.find(b => b.dataset.pick === 'new').disabled, true);
  assert.equal(buttons.find(b => b.dataset.pick === 'all').disabled, false);
}

// Shift-click ticks the range from the last click, skipping hidden rows.
{
  const rows = [row(false), row(false), row(false), row(false), row(false)];
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
  const rows = [row(false), row(false), row(false)];
  const { press, elements } = await page(rows);
  press('all');
  assert.match(elements.pickcount.textContent, /slow/);
}
console.log('albums.test: ok');
