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
  refreshPicks();
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

// The quick picks act on the rows the search left visible, so a search for
// a series followed by "without tonie" picks what is still missing of it.
// A pick sets those rows to exactly what it matches: "all" and then "without
// tonie" leaves the albums without one. Hidden rows keep their tick, so two
// searches can be combined; "none" clears every row.
const picks = [...document.querySelectorAll('[data-pick]')];
const matches = {
  all: () => true,
  without: row => row.dataset.tonie !== 'yes',
  new: row => row.dataset.new === 'yes',
};
const visible = () => rows.filter(row => !row.hidden);

// A pick that would find nothing is greyed out rather than silently idle.
function refreshPicks() {
  for (const button of picks) {
    const match = matches[button.dataset.pick];
    if (match) button.disabled = !visible().some(match);
  }
}
for (const button of picks) {
  button.addEventListener('click', () => {
    const match = matches[button.dataset.pick];
    for (const row of match ? visible() : rows) boxOf(row).checked = Boolean(match && match(row));
    updatePick();
  });
}
refreshPicks();

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
