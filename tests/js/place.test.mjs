// The parts page while stickers are placed by hand: each sticker the box
// reports is offered once, and what the server decided is shown.
import assert from 'node:assert/strict';
import { runPage, status } from './page.mjs';

const WORDS = { 'parts.place_refused': 'Nicht vergeben: {detail}',
                'parts.place_no_answer': 'Keine Antwort.' };

const page = await runPage(['app.js', 'parts.js'], {
  ids: ['printnext', 'printlink', 'placemsg'],
  pageData: { words: WORDS, placing: true, group: 'g1' },
  answers: {
    '/api/group/g1': { jobs: [] },
    '/api/batch/g1/place': { status: 'assigned', message: 'Folge 1 ist drauf.',
                             then: 'Jetzt Folge 2.', job: null },
  },
});
const places = () => page.fetched.filter(request => request.url === '/api/batch/g1/place');

await page.dispatch('box', { on_box: true, ruid: 'aaaa' });
assert.equal(places().length, 1);
assert.equal(places()[0].method, 'POST');
assert.equal(places()[0].body.fields.ruid, 'aaaa');
assert.equal(page.placemsg.textContent, 'Folge 1 ist drauf. Jetzt Folge 2.');

// Still lying there at the next poll: not offered a second time.
await page.dispatch('box', { on_box: true, ruid: 'aaaa' });
assert.equal(places().length, 1);

// Lifted, then the next sticker: offered.
await page.dispatch('box', { on_box: false, ruid: null });
await page.dispatch('box', { on_box: true, ruid: 'bbbb' });
assert.equal(places().length, 2);

// A fresh sticker teddyCloud does not know yet counts as well.
await page.dispatch('box', { on_box: false, ruid: null, unknown_ruid: 'cccc' });
assert.equal(places().at(-1).body.fields.ruid, 'cccc');

// A placement the server refused says why, instead of leaving the last
// instruction standing while someone waits at the box.
{
  const refused = await runPage(['app.js', 'parts.js'], {
    ids: ['printnext', 'printlink', 'placemsg'],
    pageData: { words: WORDS, placing: true, group: 'g1' },
    answers: { '/api/group/g1': { jobs: [] },
               '/api/batch/g1/place': status(409, { detail: 'schon belegt' }) },
  });
  refused.placemsg.textContent = 'Leg den Sticker auf.';
  await refused.dispatch('box', { on_box: true, ruid: 'aaaa' });
  assert.equal(refused.placemsg.textContent, 'Nicht vergeben: schon belegt');
}

// No answer at all says so too.
{
  const away = await runPage(['app.js', 'parts.js'], {
    ids: ['printnext', 'printlink', 'placemsg'],
    pageData: { words: WORDS, placing: true, group: 'g1' },
    answers: { '/api/group/g1': { jobs: [] }, '/api/batch/g1/place': new Error('offline') },
  });
  await away.dispatch('box', { on_box: true, ruid: 'aaaa' });
  assert.equal(away.placemsg.textContent, 'Keine Antwort.');
}
console.log('place.test: ok');
