// The script of the page that watches a sheet being made; see templates/sheet_job.html.
const data = JSON.parse(document.getElementById('page-data').textContent);
const words = data.words;
const fill = document.getElementById('fill');
const detail = document.getElementById('detail');
const ready = document.getElementById('ready');
const problem = document.getElementById('problem');
const skipped = document.getElementById('skipped');

// A second a poll: a sheet takes seconds to a minute, and the bar should move.
const POLL_MS = 1000;

async function poll() {
  let job;
  try {
    const answer = await fetch(`/api/sheet/print/${data.id}`);
    if (!answer.ok) throw new Error(String(answer.status));
    job = await answer.json();
  } catch {
    setTimeout(poll, POLL_MS);
    return;
  }
  fill.style.width = `${job.percent}%`;
  if (job.state === 'failed') {
    detail.textContent = '';
    problem.textContent = job.problem;
    problem.hidden = false;
  } else if (job.state === 'done') {
    detail.textContent = words['sheet.job_done'];
    ready.hidden = false;
    if (job.skipped.length) {
      skipped.textContent = say(words['sheet.skipped'], {names: job.skipped.join(', ')});
      skipped.hidden = false;
    }
    // The file is what was asked for; the button stays for a second copy.
    location.href = document.getElementById('download').href;
  } else {
    detail.textContent = !job.total ? words['sheet.job_waiting']
      : job.done < job.total ? say(words['sheet.job_pictures'], {done: job.done, total: job.total})
      : words['sheet.job_pdf'];
    setTimeout(poll, POLL_MS);
  }
}

poll();
