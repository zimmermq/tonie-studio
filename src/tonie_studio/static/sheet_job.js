// The script of the page that watches a sheet being made; see templates/sheet_job.html.
const data = JSON.parse(document.getElementById('page-data').textContent);
const words = data.words;
const fill = document.getElementById('fill');
const detail = document.getElementById('detail');
const outcome = document.getElementById('outcome');
const message = document.getElementById('message');
const download = document.getElementById('download');
const skipped = document.getElementById('skipped');

// A second a poll: a sheet takes seconds to a minute, and the bar should move.
const POLL_MS = 1000;

// How the work ended, in the outcome card every progress page has.
function ended(text, failed) {
  outcome.hidden = false;
  message.textContent = text;
  message.className = failed ? 'mt-0 state-failed' : 'mt-0 state-done';
}

// Asked until the sheet is done or failed; a missed answer is asked again.
window.every(POLL_MS, async () => {
  const answer = await fetch(`/api/sheet/print/${data.id}`);
  if (!answer.ok) return true;
  const job = await answer.json();
  fill.style.width = `${job.percent}%`;
  if (job.state === 'failed') {
    detail.textContent = '';
    ended(job.problem, true);
    return false;
  }
  if (job.state === 'done') {
    detail.textContent = '';
    ended(words['sheet.job_done'], false);
    download.hidden = false;
    if (job.skipped.length) {
      skipped.textContent = say(words['sheet.skipped'], {names: job.skipped.join(', ')});
      skipped.hidden = false;
    }
    // The file is what was asked for; the button stays for a second copy.
    location.href = download.href;
    return false;
  }
  detail.textContent = !job.total ? words['sheet.job_waiting']
    : job.done < job.total ? say(words['sheet.job_pictures'], {done: job.done, total: job.total})
    : words['sheet.job_pdf'];
  return true;
});
