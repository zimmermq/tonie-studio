// The page that watches a sheet being made: progress, the download, a failure.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

async function watch(state) {
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
    pageData: { id: 'x', words: { 'sheet.job_waiting': 'waiting',
                                  'sheet.job_pictures': '{done} of {total}',
                                  'sheet.job_pdf': 'pdf', 'sheet.job_done': 'done',
                                  'sheet.skipped': 'left off: {names}' } },
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
