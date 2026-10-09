// The parts page: a group that is gone stops being asked about.
import assert from 'node:assert/strict';
import { runPage } from './page.mjs';

const page = await runPage(['app.js', 'parts.js'], {
  ids: ['printnext', 'printlink'],
  pageData: { words: { 'job.state.done': 'fertig' }, placing: false, group: 'gone' },
  answers: {},
});

assert.equal(page.timers.length, 0);
console.log('parts.test: ok');
