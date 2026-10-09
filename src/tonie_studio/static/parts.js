// The script of the parts page; see templates/parts.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

// Updated in place rather than by reloading the page, which would throw it
// back to the top every few seconds.
const states = Object.fromEntries(Object.entries(
  data.words
).map(([key, text]) => [key.replace('job.state.', ''), text]));
// Once every encode has ended the walkthrough moves on to the stickers,
// and the tonies made so far can go to the sheet. While stickers are being
// placed the page keeps looking, because a tonie appears with each one.
const placingStickers = data.placing;
const printnext = document.getElementById('printnext');
function moveTo(step) {
  for (const item of document.querySelectorAll('.steps li')) {
    const here = item.dataset.step === step;
    item.toggleAttribute('aria-current', here);
    if (here) item.setAttribute('aria-current', 'step');
    item.classList.toggle('done', Number(item.dataset.step) < Number(step));
  }
}
const group = data.group;
every(3000, async () => {
  const response = await fetch(`/api/group/${group}`);
  // A group that is gone stays gone; there is nothing left to watch.
  if (!response.ok) return false;
  const answer = await response.json();
  let running = false;
  let waitingForSticker = false;
  const models = [];
  for (const job of answer.jobs) {
    const row = document.querySelector(`[data-job="${job.id}"]`);
    if (!row) continue;
    if (job.model) models.push(job.model);
    if (job.state === 'done' && !job.assigned) waitingForSticker = true;
    const text = states[job.state] || job.state;
    row.querySelector('.state').textContent =
      job.percent != null && job.state === 'running' ? `${text}, ${job.percent} %` : text;
    row.querySelector('.assign').hidden = job.state !== 'done' || job.assigned;
    row.querySelector('.assigned').hidden = !job.assigned;
    row.querySelector('.note').textContent = job.note || '';
    if (job.state === 'queued' || job.state === 'running') running = true;
  }
  if (!running) {
    moveTo('4');
    if (printnext && models.length) {
      document.getElementById('printlink').href =
        '/sheet?' + models.map(model => `model=${encodeURIComponent(model)}`).join('&');
      printnext.hidden = false;
    }
  }
  return running || (placingStickers && waitingForSticker);
});

if (data.placing) {
// Each sticker put on the box goes to the next episode; the server decides,
// the page only reports what the box sees. A sticker is asked about again
// only after it was taken off, or while teddyCloud has not filed it yet.
const placemsg = document.getElementById('placemsg');
let lastSticker = null;
// One sticker at a time: a box report arriving while the last placement is
// still being answered waits for the next one.
let asking = false;
document.addEventListener('box', async event => {
  if (asking) return;
  asking = true;
  try {
    const box = event.detail;
    const ruid = box.unknown_ruid || (box.on_box ? box.ruid : null);
    if (!ruid) lastSticker = null;
    if (ruid && ruid !== lastSticker) {
      const body = new FormData();
      body.append('ruid', ruid);
      const answer = await (await fetch(`/api/batch/${group}/place`, {method: 'POST', body})).json();
      placemsg.textContent = `${answer.message} ${answer.then}`;
      if (answer.status !== 'unknown') lastSticker = ruid;
      if (answer.job) {
        const row = document.querySelector(`[data-job="${answer.job}"] .note`);
        if (row) row.textContent = answer.message;
      }
    }
  } catch (error) {
    // A dropped answer changes nothing; the next box report asks again.
  }
  asking = false;
});
}
