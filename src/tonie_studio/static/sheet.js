// The script of the sheet page; see templates/sheet.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

const words = data.words;
const rows = [...document.querySelectorAll('.sticker')];
const total = document.getElementById('total');

// How the covers are cut: it decides how many fit on a sheet and how big the
// text side is. Remembered in this browser, since a person owns one punch or
// none; storage may be off, and the page then starts with the first choice.
const LAYOUT_KEY = 'sheet.layout';
const layoutChoices = [...document.querySelectorAll('input[name=layout]')];
const layoutOf = () => (layoutChoices.find(choice => choice.checked) || {}).value || 'sheet';
const layout = () => data.layouts[layoutOf()] || Object.values(data.layouts)[0];
try {
  const remembered = layoutChoices.find(choice => choice.value === localStorage.getItem(LAYOUT_KEY));
  if (remembered) for (const choice of layoutChoices) choice.checked = choice === remembered;
} catch { /* not remembered */ }
const layoutChanged = [];
function showLayout() {
  for (const intro of document.querySelectorAll('[data-layout]')) {
    intro.hidden = intro.dataset.layout !== layoutOf();
  }
  for (const step of layoutChanged) step();
}
for (const choice of layoutChoices) {
  choice.addEventListener('change', () => {
    try { localStorage.setItem(LAYOUT_KEY, layoutOf()); } catch { /* not remembered */ }
    showLayout();
  });
}

// Both circles show the same cover, each rendered in its own mode, so the
// choice is made by looking rather than by reading a description of it. The
// picture is only asked for once its row comes into view: a page of every
// tonie would otherwise load hundreds of full-size covers at once.
function showCover(row) {
  if (row.dataset.shown) return;
  row.dataset.shown = 'yes';
  const url = `url("${row.dataset.src}")`;
  for (const layer of row.querySelectorAll('.preview span')) layer.style.backgroundImage = url;
}
const watcher = typeof IntersectionObserver === 'function'
  ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) { showCover(entry.target); watcher.unobserve(entry.target); }
      }
    }, { rootMargin: '400px' })
  : null;

// A row's choice travels in one field, "count:mode:model", so a sheet of
// every tonie stays under the number of fields a request may carry. An
// unticked row's field is switched off and not sent at all.
const pickOf = row => row.querySelector('input[name=picks]');
const modelOf = row => pickOf(row).dataset.model;
const countOf = row => +pickOf(row).value.split(':')[0] || 0;
const modeOf = row => pickOf(row).value.split(':')[1];
function setPick(row, count, mode) {
  const pick = pickOf(row);
  pick.value = `${count}:${mode}:${modelOf(row)}`;
  pick.disabled = count < 1;
}

for (const row of rows) {
  if (watcher) watcher.observe(row); else showCover(row);
  for (const radio of row.querySelectorAll('input[type=radio]')) {
    radio.addEventListener('change', () => {
      setPick(row, countOf(row), radio.value);
      row.querySelectorAll('.mode').forEach(mode => mode.classList.remove('chosen'));
      radio.closest('.mode').classList.add('chosen');
    });
  }
}

function setCount(row, value) {
  value = Math.max(0, Math.min(data.mostCopies, value));
  setPick(row, value, modeOf(row));
  row.querySelector('.want').checked = value > 0;
  row.querySelector('.times').classList.toggle('empty', value < 1);
  row.querySelector('.count').textContent = value;
  refresh();
}

function refresh() {
  const sum = rows.reduce((n, row) => n + countOf(row), 0);
  const sheets = Math.ceil(sum / layout().perPage);
  total.textContent = sum
    ? say(words['sheet.total'], {count: sum}) + ', ' + say(words['sheet.sheets'], {count: sheets})
    : words['sheet.nothing_chosen'];
}

for (const row of rows) {
  row.querySelector('.want').addEventListener('change', event => {
    setCount(row, event.target.checked ? 1 : 0);
  });
  row.querySelectorAll('button[data-step]').forEach(button => {
    button.addEventListener('click', () => setCount(row, countOf(row) + +button.dataset.step));
  });
}

// The search. Names are matched word by word, every word having to fit. A
// comma or a new line starts another search whose hits are added, and a run
// of UIDs separated by spaces counts as one search per UID: chips are read
// off one after the other, often typed or pasted in one go. Colons in a UID
// are left out on both sides, since chips print it either way.
const isUid = word => /^[0-9a-f]{6,}$/.test(word);
function searches(text) {
  return text.toLowerCase().split(/[,;\n]+/).map(part => part.trim()).filter(Boolean)
    .flatMap(part => {
      const words = part.split(/\s+/).map(word => word.replace(/:/g, ''));
      return words.length > 1 && words.every(isUid) ? words.map(word => [word]) : [words];
    });
}

const find = document.getElementById('find');
const onlyticked = document.getElementById('onlyticked');
const shown = document.getElementById('shown');
const nohits = document.getElementById('nohits');

function filter() {
  if (!find) return;
  const wanted = searches(find.value);
  let count = 0;
  for (const row of rows) {
    const hit = !wanted.length
      || wanted.some(words => words.every(word => row.dataset.search.includes(word)));
    row.hidden = !(hit && (!onlyticked.checked || countOf(row) > 0));
    if (!row.hidden) count += 1;
  }
  shown.textContent = count === rows.length ? ''
    : say(words['sheet.shown_of'], {shown: count, count: rows.length});
  nohits.hidden = count > 0 || !rows.length;
}

if (find) {
  find.addEventListener('input', filter);
  // Enter would send the whole form, and with it a sheet nobody asked for yet.
  find.addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
  onlyticked.addEventListener('change', filter);
  document.getElementById('tickshown').addEventListener('click', () => {
    for (const row of rows) if (!row.hidden && countOf(row) < 1) setCount(row, 1);
  });
  document.getElementById('untick').addEventListener('click', () => {
    for (const row of rows) setCount(row, 0);
    filter();
  });
}

const fillsheet = document.getElementById('fillsheet');
if (fillsheet) fillsheet.addEventListener('click', () => {
  // Spread a whole sheet over what is ticked, the remainder onto the first.
  const chosen = rows.filter(row => countOf(row) > 0);
  const spread = chosen.length ? chosen : rows;
  const perPage = layout().perPage;
  const each = Math.floor(perPage / spread.length);
  spread.forEach((row, index) => {
    setCount(row, each + (index === 0 ? perPage % spread.length : 0));
  });
});

const extra = document.getElementById('extra');
if (extra) {
  showPicked(extra, document.getElementById('extrapicked'),
             files => say(words['sheet.pictures'], {count: files.length}));
}

// The text side. The server decides which lines fit, so this only has to
// draw them: a preview that re-decided would promise what the sheet does not
// deliver. The lines are asked for when shown, per model and layout.
const disc = document.getElementById('backdisc');
if (disc) {
  const backs = {};
  async function backOf(model) {
    const key = `${model} ${layoutOf()}`;
    if (!(key in backs)) {
      const params = new URLSearchParams({model, layout: layoutOf()});
      const answer = await fetch(`/api/sheet/back?${params}`);
      backs[key] = answer.ok ? await answer.json() : null;
    }
    return backs[key];
  }
  const who = document.getElementById('backwho');
  const wantback = document.getElementById('wantback');
  const wanttracks = document.getElementById('wanttracks');
  // The circle is drawn at this many pixels for a sticker of this many
  // millimetres, so a point on screen covers what it covers on paper.
  const DISC_PX = 240;
  const pxPerPt = () => DISC_PX / (layout().diameterMm / 25.4 * 72);

  async function drawBack() {
    const row = rows.find(row => countOf(row) > 0) || rows[0];
    const model = row && modelOf(row);
    const back = model && await backOf(model);
    if (!back) { disc.style.display = 'none'; return; }
    const PX_PER_PT = pxPerPt();

    disc.style.display = '';
    // The same white the server mixes into the cover before printing, laid
    // over the picture as a flat sheet rather than guessed at.
    const fade = `rgba(255, 255, 255, ${data.backFade})`;
    disc.style.backgroundImage =
      `linear-gradient(${fade}, ${fade}), url("${row.dataset.src}")`;
    disc.innerHTML = '';
    for (const line of back[wanttracks.checked ? 'tracks' : 'plain']) {
      const span = document.createElement('span');
      span.textContent = line.text;
      // The blank line between the heading and the list is a gap, not a
      // character: an empty span needs its height stated.
      if (!line.text) span.style.height = `${line.size * PX_PER_PT * 1.22}px`;
      span.style.fontSize = `${line.size * PX_PER_PT}px`;
      span.style.lineHeight = '1.22';
      if (line.bold) span.style.fontWeight = '600';
      disc.appendChild(span);
    }
    who.textContent = row.querySelector('strong').textContent;
  }

  function showBack() {
    const on = wantback.checked;
    disc.parentElement.style.opacity = on ? '1' : '.35';
    wanttracks.disabled = !on;
    if (on) drawBack();
  }

  wantback.addEventListener('change', showBack);
  wanttracks.addEventListener('change', drawBack);
  for (const row of rows) {
    row.querySelector('.want').addEventListener('change', () => {
      if (wantback.checked) drawBack();
    });
  }
  layoutChanged.push(() => { if (wantback.checked) drawBack(); });
  drawBack();
  showBack();
}

layoutChanged.push(refresh);
showLayout();
for (const row of rows) setCount(row, countOf(row));
refresh();
filter();
