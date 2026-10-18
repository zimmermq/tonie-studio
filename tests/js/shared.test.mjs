// The helpers app.js gives every page: searching rows, and saying how work ended.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

const page = await runPage(['app.js'], { ids: ['outcome', 'message'] });
const { searchWords, matches, ended } = page.window;

// A search is its words, lowercased, with the colons of a UID left out.
assert.deepEqual([...searchWords('  Bibi E0:04:03 ')], ['bibi', 'e00403']);

// A row matches when every word is in it, in any order.
const row = element('row', '', { dataset: { search: 'bibi blocksberg e0040350' } });
assert.equal(matches(row, searchWords('blocksberg BIBI')), true);
assert.equal(matches(row, searchWords('bibi tina')), false);
assert.equal(matches(row, []), true);

// The outcome card shows, and the message says whether it failed.
page.outcome.hidden = true;
ended('broke', true);
assert.equal(page.outcome.hidden, false);
assert.equal(page.message.textContent, 'broke');
assert.equal(page.message.className, 'mt-0 state-failed');
ended('done', false);
assert.equal(page.message.className, 'mt-0 state-done');
