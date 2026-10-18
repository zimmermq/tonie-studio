// The listening page takes over each row's new figures without moving it.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

// The next rendering, as the page would parse it.
function rendering(rows, live = '') {
  return { getElementById: id => (id === 'live' ? { innerHTML: live } : null),
           querySelectorAll: () => rows };
}

const row = (id, played, html) => Object.assign(
  element(id, '', { dataset: { search: id, played: String(played) } }), { innerHTML: html });

{
  const here = row('heard-a', 0, 'never');
  here.hidden = true;  // filtered away by the list
  const there = row('heard-a', 120, '2 min');
  const live = Object.assign(element('live'), { innerHTML: 'since Monday' });
  await runPage(['app.js', 'listening.js'], {
    prepared: { 'heard-a': here, live },
    answers: { 'http://localhost/page': '<html>' },
    globals: {
      DOMParser: class { parseFromString() { return rendering([there], 'since Sunday'); } },
    },
  });
  assert.equal(here.innerHTML, '2 min');
  assert.equal(here.dataset.played, '120');
  assert.equal(here.hidden, true);
  assert.equal(live.innerHTML, 'since Sunday');
}
