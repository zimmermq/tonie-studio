// The script of the job page; see templates/job.html.
const data = pageData();

const id = data.id;
const words = data.words;
const state = document.getElementById('state');
const detail = document.getElementById('detail');

// The encode counts in seconds of audio, the uploads in files. Showing
// "encode 312/4917" would be true and useless.
function describe(job) {
  if (!job.step) return words['job.preparing'];
  if (job.step === 'upload') return say(words['job.uploaded'], {done: job.done, count: job.total});
  if (job.step === 'encode') {
    const mins = value => Math.round(value / 60);
    return job.total > 90
      ? say(words['job.encoded_minutes'], {done: mins(job.done), count: mins(job.total)})
      : words['job.encoding'];
  }
  return `${job.step} ${job.done}/${job.total}`;
}

// ``job`` is the answer of /api/job; ``data`` stays the page's own.
function show(job) {
  state.textContent = words[`job.state.${job.state}`] || job.state;
  progressTo(job.percent ?? 0);
  detail.textContent = describe(job);

  if (job.state === 'done' || job.state === 'failed') {
    const next = document.getElementById('next');
    next.hidden = job.state !== 'done';
    // A batch encode may have gone on its sticker by itself since the page
    // was drawn; then it says so and leads on to the tonies.
    const ending = job.assigned && data.assigned ? data.assigned : null;
    if (ending) {
      next.href = ending.href;
      next.textContent = words[ending.nextKey];
    }
    ended(job.error || words[(ending || data).doneKey], job.state === 'failed');
    return false;
  }
  return true;
}

if (data.running) {
  every(1000, async () => {
    const response = await ask(`/api/job/${id}`);
    // A job that is gone stays gone; there is nothing left to watch.
    return response.ok ? show(await response.json()) : false;
  });
}
