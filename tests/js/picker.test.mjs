// The sticker picker's search, shared by every page that chooses a sticker.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

function picker(searches) {
  const rows = searches.map(search => element('', '', { dataset: { search } }));
  const none = element('');
  none.hidden = true;
  const box = element('', '', { children: { '.picker-none': none } });
  box.querySelectorAll = () => rows;
  const find = element('');
  find.value = '';
  find.classList.contains = name => name === 'picker-find';
  find.closest = () => box;
  return { rows, none, find };
}

const STICKERS = ['simba e0:04:03:50:1f:dd:f0:44 e00403501fddf044',
                  'free e0:04:03:50:0e:ee:34:61 e00403500eee3461'];

// A UID is found as printed, with colons, or by its last digits.
{
  const { rows, find } = picker(STICKERS);
  const page = await runPage(['app.js']);
  find.value = 'E0:04:03:50:0E:EE:34:61';
  await page.dispatch('input', undefined, { target: find });
  assert.deepEqual(rows.map(row => row.hidden), [true, false]);
  find.value = '1fddf044';
  await page.dispatch('input', undefined, { target: find });
  assert.deepEqual(rows.map(row => row.hidden), [false, true]);
}

// Nothing found says so; typing elsewhere changes nothing.
{
  const { rows, none, find } = picker(STICKERS);
  const page = await runPage(['app.js']);
  find.value = 'nothing like it';
  await page.dispatch('input', undefined, { target: find });
  assert.equal(none.hidden, false);
  const other = element('');
  other.classList.contains = () => false;
  await page.dispatch('input', undefined, { target: other });
  assert.deepEqual(rows.map(row => row.hidden), [true, true]);
}
