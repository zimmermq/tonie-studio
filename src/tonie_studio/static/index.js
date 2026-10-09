// The script of the index page; see templates/index.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

// Filtering happens over the rendered rows. At sixteen albums a round trip
// per keystroke would be slower than the browser is; if the archive ever
// grows to hundreds, this becomes a server-side filter.
const words = data.words;
const search = document.getElementById('search');
const rows = [...document.querySelectorAll('#albums .pickrow')];
const count = document.getElementById('count');
const nohits = document.getElementById('nohits');

search.addEventListener('input', () => {
  const terms = search.value.toLowerCase().split(/\s+/).filter(Boolean);
  let shown = 0;
  for (const row of rows) {
    const hit = terms.every(term => row.dataset.search.includes(term));
    row.hidden = !hit;
    if (hit) shown++;
  }
  count.textContent = shown === rows.length
    ? say(words['albums.count'], {count: rows.length})
    : say(words['albums.shown_of'], {shown, count: rows.length});
  nohits.hidden = shown > 0;
});

// Checking an album reads every track's tags, over a network share a
// second or more per album; without a word the click looks ignored.
const loading = document.getElementById('loading');
function showLoading() { loading.hidden = false; }
for (const link of document.querySelectorAll('#albums a.album')) {
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
document.getElementById('albums').addEventListener('change', () => {
  const ticked = document.querySelectorAll('#albums input[name=ids]:checked').length;
  pick.hidden = ticked === 0;
  pickcount.textContent = say(words['batch.picked'], {count: ticked});
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
