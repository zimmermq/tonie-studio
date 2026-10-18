// The parts page: a group that is gone stops being asked about, and says so
// instead of standing at "running, 40 %" for ever.
import assert from 'node:assert/strict';
import { element, runPage, status } from './page.mjs';

const hiddenNote = () => Object.assign(element('gone'), { hidden: true });

const page = await runPage(['app.js', 'parts.js'], {
  ids: ['printnext', 'printlink'],
  prepared: { gone: hiddenNote() },
  pageData: { words: { 'job.state.done': 'fertig' }, placing: false, group: 'gone' },
  answers: {},
});

assert.equal(page.timers.length, 0);
assert.equal(page.gone.hidden, false);
console.log('parts.test: ok');

// Any other failed answer - the app restarting right now - is asked again.
const busy = await runPage(['app.js', 'parts.js'], {
  ids: ['printnext', 'printlink'],
  prepared: { gone: hiddenNote() },
  pageData: { words: {}, placing: false, group: 'g1' },
  answers: { '/api/group/g1': status(502) },
});

assert.equal(busy.timers.length, 1);
assert.equal(busy.gone.hidden, true);
console.log('parts.test: retry ok');
