// The script of the job page; see templates/job.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

const id = data.id;
const words = data.words;
const fill = document.getElementById('fill');
const state = document.getElementById('state');
const detail = document.getElementById('detail');
const outcome = document.getElementById('outcome');
const message = document.getElementById('message');

// The encode counts in seconds of audio, the uploads in files. Showing
// "encode 312/4917" would be true and useless.
function describe(data) {
  if (!data.step) return words['job.preparing'];
  if (data.step === 'upload') return say(words['job.uploaded'], {done: data.done, count: data.total});
  if (data.step === 'encode') {
    const mins = value => Math.round(value / 60);
    return data.total > 90
      ? say(words['job.encoded_minutes'], {done: mins(data.done), count: mins(data.total)})
      : words['job.encoding'];
  }
  return `${data.step} ${data.done}/${data.total}`;
}

function show(data) {
  state.textContent = words[`job.state.${data.state}`] || data.state;
  fill.style.width = (data.percent ?? 0) + '%';
  detail.textContent = describe(data);

  if (data.state === 'done' || data.state === 'failed') {
    outcome.hidden = false;
    document.getElementById('next').hidden = data.state !== 'done';
    message.textContent = data.error || words[data.doneKey];
    message.className = data.state === 'failed' ? 'state-failed' : 'state-done';
    return false;
  }
  return true;
}

if (data.running) {
  every(1000, async () => {
    const response = await fetch(`/api/job/${id}`);
    // A job that is gone stays gone; there is nothing left to watch.
    return response.ok ? show(await response.json()) : false;
  });
}
