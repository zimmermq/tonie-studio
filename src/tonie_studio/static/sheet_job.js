// The script of the page that watches a sheet being made; see templates/sheet_job.html.
const data = pageData();
const words = data.words;
const fill = document.getElementById('fill');
const detail = document.getElementById('detail');
const download = document.getElementById('download');
const skipped = document.getElementById('skipped');

// A second a poll: a sheet takes seconds to a minute, and the bar should move.
const POLL_MS = 1000;

// Asked until the sheet is done or failed; a missed answer is asked again,
// but a sheet the server no longer knows - it restarted - is gone for good.
window.every(POLL_MS, async () => {
  const answer = await ask(`/api/sheet/print/${data.id}`);
  if (answer.status === 404) {
    detail.textContent = '';
    ended(words['sheet.job_gone'], true);
    return false;
  }
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
    // A page opened on a finished sheet, by the back button say, only offers it.
    if (!data.finished) location.href = download.href;
    return false;
  }
  detail.textContent = !job.total ? words['sheet.job_waiting']
    : job.done < job.total ? say(words['sheet.job_pictures'], {done: job.done, total: job.total})
    : words['sheet.job_pdf'];
  return true;
});
