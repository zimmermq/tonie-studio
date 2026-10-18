// The page that watches a sheet being made: progress, the download, a failure.
import assert from 'node:assert/strict';
import { element, runPage, status } from './page.mjs';

async function watch(state, extra = {}) {
  const download = element('download');
  download.href = '/sheet/print/x/pdf';
  download.hidden = true;
  const outcome = element('outcome');
  outcome.hidden = true;
  const skipped = element('skipped');
  skipped.hidden = true;
  return runPage(['app.js', 'sheet_job.js'], {
    ids: ['fill', 'detail', 'message'],
    prepared: { download, outcome, skipped },
    pageData: { id: 'x', ...extra,
                words: { 'sheet.job_waiting': 'waiting',
                         'sheet.job_pictures': '{done} of {total}',
                         'sheet.job_pdf': 'pdf', 'sheet.job_done': 'done',
                         'sheet.skipped': 'left off: {names}', 'sheet.job_gone': 'gone' } },
    answers: { '/api/sheet/print/x': state },
  });
}

// While pictures are fetched it counts them and asks again.
{
  const page = await watch({ state: 'running', percent: 30, done: 3, total: 10, skipped: [] });
  assert.equal(page.detail.textContent, '3 of 10');
  assert.equal(page.fill.style.width, '30%');
  assert.equal(page.outcome.hidden, true);
  assert.equal(page.timers.length, 1);
}

// Done: the outcome says so, the download starts and its button shows; what
// was left off is named.
{
  const page = await watch({ state: 'done', percent: 100, done: 2, total: 2, skipped: ['Wolf'] });
  assert.equal(page.outcome.hidden, false);
  assert.equal(page.message.textContent, 'done');
  assert.equal(page.message.className, 'mt-0 state-done');
  assert.equal(page.download.hidden, false);
  assert.equal(page.location.href, '/sheet/print/x/pdf');
  assert.equal(page.skipped.textContent, 'left off: Wolf');
  assert.equal(page.timers.length, 0);
}

// A failure says why in the outcome, offers no download and stops asking.
{
  const page = await watch({ state: 'failed', percent: 0, done: 0, total: 0, skipped: [],
                             problem: 'Nothing selected to print.' });
  assert.equal(page.outcome.hidden, false);
  assert.equal(page.message.textContent, 'Nothing selected to print.');
  assert.equal(page.message.className, 'mt-0 state-failed');
  assert.equal(page.download.hidden, true);
  assert.equal(page.timers.length, 0);
}

// Opened again once the sheet is done - by the back button, say - it shows
// the download but does not start it a second time.
{
  const page = await watch({ state: 'done', percent: 100, done: 2, total: 2, skipped: [] },
                           { finished: true });
  assert.equal(page.download.hidden, false);
  assert.notEqual(page.location.href, '/sheet/print/x/pdf');
}

// A sheet the server no longer knows - the app restarted - ends the watching
// and says so, rather than waiting for ever.
{
  const page = await watch(status(404));
  assert.equal(page.message.textContent, 'gone');
  assert.equal(page.message.className, 'mt-0 state-failed');
  assert.equal(page.timers.length, 0);
}

// Any other bad answer is a hiccup: it asks again.
{
  const page = await watch(status(502));
  assert.equal(page.outcome.hidden, true);
  assert.equal(page.timers.length, 1);
}

