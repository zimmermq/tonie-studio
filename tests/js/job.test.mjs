// The job page: once the watched job finishes, it says so.
import assert from 'node:assert/strict';
import { runPage, status } from './page.mjs';

const words = { 'job.state.done': 'fertig', 'job.taf_done': 'Encodiert.', 'job.preparing': '...' };
const page = await runPage(['app.js', 'job.js'], {
  ids: ['fill', 'state', 'detail', 'outcome', 'message', 'next'],
  pageData: { id: 'abc', words, doneKey: 'job.taf_done', running: true },
  answers: { '/api/job/abc': { state: 'done', percent: 100, step: null, error: null } },
});

assert.equal(page.message.textContent, 'Encodiert.');
assert.equal(page.outcome.hidden, false);
console.log('job.test: ok');

// A batch encode that went on its sticker by itself no longer asks to be assigned.
const placed = await runPage(['app.js', 'job.js'], {
  ids: ['fill', 'state', 'detail', 'outcome', 'message', 'next'],
  pageData: {
    id: 'def', words: { ...words, 'job.published': 'Auf dem Sticker.', 'job.to_tonies': 'Zu den Tonies' },
    doneKey: 'job.taf_done', running: true,
    assigned: { doneKey: 'job.published', href: '/tonies', nextKey: 'job.to_tonies' },
  },
  answers: { '/api/job/def': { state: 'done', percent: 100, step: null, error: null, assigned: true } },
});

assert.equal(placed.message.textContent, 'Auf dem Sticker.');
assert.equal(placed.next.href, '/tonies');
assert.equal(placed.next.textContent, 'Zu den Tonies');
console.log('job.test: assigned ok');

// A job the server no longer knows - it restarted - is gone, and says so.
const gone = await runPage(['app.js', 'job.js'], {
  ids: ['fill', 'state', 'detail', 'outcome', 'message', 'next'],
  pageData: { id: 'old', words: { ...words, 'job.gone': 'Nicht mehr bekannt.' },
              doneKey: 'job.taf_done', running: true },
  answers: {},
});

assert.equal(gone.message.textContent, 'Nicht mehr bekannt.');
assert.equal(gone.outcome.hidden, false);
assert.equal(gone.timers.length, 0);
console.log('job.test: gone ok');

// Any other failed answer is asked again.
const busy = await runPage(['app.js', 'job.js'], {
  ids: ['fill', 'state', 'detail', 'outcome', 'message', 'next'],
  pageData: { id: 'x', words, doneKey: 'job.taf_done', running: true },
  answers: { '/api/job/x': status(502) },
});

assert.equal(busy.timers.length, 1);
console.log('job.test: retry ok');

// A finished job is past its last step: the line under the bar says no more
// than the state does, rather than "encoding" beside "done".
for (const state of ['done', 'failed']) {
  const ended = await runPage(['app.js', 'job.js'], {
    ids: ['fill', 'state', 'detail', 'outcome', 'message', 'next'],
    pageData: { id: 'e', words: { ...words, 'job.encoding': 'wird encodiert' },
                doneKey: 'job.taf_done', running: true },
    answers: { '/api/job/e': { state, percent: 100, step: 'encode', done: 30, total: 60,
                               error: state === 'failed' ? 'kaputt' : null } },
  });
  assert.equal(ended.detail.textContent, '');
}
console.log('job.test: ended ok');
