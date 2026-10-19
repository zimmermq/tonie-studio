// The script of the index page; see templates/index.html.
const data = pageData();

// Search, filter and order are the shared list's (list.js).
const words = data.words;
const rows = [...document.querySelectorAll('#albums .pickrow')];

// Checking an album reads every track's tags, over a network share a
// second or more per album; without a word the click looks ignored.
const loading = document.getElementById('loading');
function showLoading() { loading.hidden = false; }
for (const link of document.querySelectorAll('#albums a.item')) {
  link.addEventListener('click', event => {
    if (!(event.metaKey || event.ctrlKey || event.shiftKey)) showLoading();
  });
}
document.getElementById('pick').addEventListener('submit', showLoading);
// A page restored from the back/forward cache must not keep the hint.
window.addEventListener('pageshow', () => { loading.hidden = true; });

// Ticking albums gathers them for one batch: a series of episodes set up
// together instead of one album page after another.
const pick = document.getElementById('pick');
const pickcount = document.getElementById('pickcount');
const boxOf = row => row.querySelector('input[name=ids]');

function updatePick() {
  const ticked = rows.filter(row => boxOf(row).checked).length;
  pick.hidden = ticked === 0;
  // Setting up reads every album's tags, about a second each over a share;
  // from data.slowPick albums on, the wait deserves a word before the click.
  const slow = ticked >= data.slowPick ? ` ${say(words['albums.pick_slow'], {count: ticked})}` : '';
  pickcount.textContent = say(words['batch.picked'], {count: ticked}) + slow;
}
document.getElementById('albums').addEventListener('change', updatePick);

// Like on the print page, ticking acts on what the search and the chips
// leave in view, and a tick stays when the view changes: a series searched
// for, "without a tonie" chosen, all of those ticked, then the next series.
const tickshown = document.getElementById('tickshown');
if (tickshown) {
  tickshown.addEventListener('click', () => {
    for (const row of rows) if (!row.hidden) boxOf(row).checked = true;
    updatePick();
  });
  document.getElementById('untick').addEventListener('click', () => {
    for (const row of rows) boxOf(row).checked = false;
    updatePick();
  });
}

// Shift-click ticks or clears the visible rows between the last click and
// this one, as file managers do: episodes 3 to 9 in two clicks.
let anchor = null;
rows.forEach((row, index) => {
  boxOf(row).addEventListener('click', event => {
    if (event.shiftKey && anchor !== null) {
      const state = boxOf(row).checked;
      for (const other of rows.slice(Math.min(anchor, index), Math.max(anchor, index) + 1)) {
        if (!other.hidden) boxOf(other).checked = state;
      }
      updatePick();
    }
    anchor = index;
  });
});

// Two pickers, a folder and single files, of which one counts: choosing in
// one empties the other, and the button waits until something is chosen.
const form = document.getElementById('upload');
const picked = document.getElementById('picked');
const send = document.getElementById('send');
const pickers = [...form.querySelectorAll('input[type=file]')];

for (const input of pickers) {
  showPicked(input, picked, files => {
    const bytes = files.reduce((sum, file) => sum + file.size, 0);
    return say(words['upload.picked'], {count: files.length, size: (bytes / 1048576).toFixed(0)});
  });
  input.addEventListener('change', () => {
    for (const other of pickers) {
      if (other !== input) other.value = '';
    }
    send.disabled = !input.files.length;
  });
}

// Sent by hand rather than by the form, so the page can say how far it is: a
// folder of FLACs takes minutes, and a page that shows nothing meanwhile gets
// the button pressed again. The server answers with where to go next, the
// batch page; a refusal comes as its page, whose message is shown here, on
// the album list. Writing that page over this one would run app.js a second
// time, whose top-level names then clash and stop every script on it, and
// leave no history entry, so "back" would skip the album list as well.
const refusal = document.getElementById('refusal');
function refusedWith(text) {
  const page = new DOMParser().parseFromString(text, 'text/html');
  const message = page.querySelector('main > .warn');
  return message && message.textContent.trim() ? message.textContent.trim() : words['upload.failed'];
}
form.addEventListener('submit', event => {
  event.preventDefault();
  const request = new XMLHttpRequest();
  const settled = () => { window.onbeforeunload = null; };
  const failed = () => {
    settled();
    picked.textContent = words['upload.failed'];
    send.disabled = false;
  };
  refusal.hidden = true;
  request.open('POST', form.action || '/upload');
  request.setRequestHeader('X-Upload', '1');
  request.upload.onprogress = progress => {
    if (!progress.lengthComputable) return;
    const percent = Math.floor(progress.loaded / progress.total * 100);
    picked.textContent = say(words['upload.sending'], {percent});
  };
  request.onload = () => {
    settled();
    if (request.status >= 200 && request.status < 300) {
      location.href = JSON.parse(request.responseText).next;
      return;
    }
    refusal.textContent = refusedWith(request.responseText);
    refusal.hidden = false;
    picked.textContent = words['upload.failed'];
    send.disabled = false;
  };
  request.onerror = failed;
  send.disabled = true;
  // Leaving now would cancel the upload half way.
  window.onbeforeunload = leaving => { leaving.preventDefault(); };
  request.send(new FormData(form));
});
