// The import page: while a run is active it reloads once the run moves on.
import assert from 'node:assert/strict';
import { runPage } from './page.mjs';

const answers = { '/api/import': { phase: 'running', counts: { pending: 2 } } };
const page = await runPage(['app.js', 'import.js'], { pageData: { watch: true }, answers });

assert.equal(page.fetched.length, 1);
assert.equal(page.timers.length, 1);
page.timers.shift()();
await new Promise(done => setImmediate(done));
assert.equal(page.location.reloaded, 0, 'nothing changed, so no reload');

answers['/api/import'] = { phase: 'running', counts: { pending: 1 } };
page.timers.shift()();
for (let round = 0; round < 5; round++) await new Promise(done => setImmediate(done));
assert.equal(page.location.reloaded, 1);
assert.equal(page.timers.length, 0, 'stops watching once it reloads');
console.log('import.test: ok');

// A finished run, or none, is not watched at all.
const idle = await runPage(['app.js', 'import.js'], { pageData: { watch: false }, answers });
assert.equal(idle.fetched.length, 0);
console.log('import.test: idle ok');
