// The job page: once the watched job finishes, it says so.
import assert from 'node:assert/strict';
import { runPage } from './page.mjs';

const words = { 'job.state.done': 'fertig', 'job.taf_done': 'Encodiert.', 'job.preparing': '...' };
const page = await runPage(['app.js', 'job.js'], {
  ids: ['fill', 'state', 'detail', 'outcome', 'message', 'next'],
  pageData: { id: 'abc', words, doneKey: 'job.taf_done', running: true },
  answers: { '/api/job/abc': { state: 'done', percent: 100, step: null, error: null } },
});

assert.equal(page.message.textContent, 'Encodiert.');
assert.equal(page.outcome.hidden, false);
console.log('job.test: ok');
