// The script of the sheet page; see templates/sheet.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

const perPage = data.perPage;
const words = data.words;
const rows = [...document.querySelectorAll('.sticker')];
const total = document.getElementById('total');

// Both circles show the same cover, each rendered in its own mode, so the
// choice is made by looking rather than by reading a description of it.
for (const row of rows) {
  const url = `url("${row.dataset.src}")`;
  for (const layer of row.querySelectorAll('.preview span')) layer.style.backgroundImage = url;
  for (const radio of row.querySelectorAll('input[type=radio]')) {
    radio.addEventListener('change', () => {
      row.querySelector('input[name=modes]').value = radio.value;
      row.querySelectorAll('.mode').forEach(mode => mode.classList.remove('chosen'));
      radio.closest('.mode').classList.add('chosen');
    });
  }
}

const countOf = row => +row.querySelector('input[name=counts]').value || 0;

function setCount(row, value) {
  value = Math.max(0, Math.min(data.mostCopies, value));
  row.querySelector('input[name=counts]').value = value;
  row.querySelector('.want').checked = value > 0;
  row.querySelector('.times').classList.toggle('empty', value < 1);
  row.querySelector('.count').textContent = value;
  refresh();
}

function refresh() {
  const sum = rows.reduce((n, row) => n + countOf(row), 0);
  const sheets = Math.ceil(sum / perPage);
  total.textContent = sum
    ? say(words['sheet.total'], {count: sum}) + ', ' + say(words['sheet.sheets'], {count: sheets})
    : words['sheet.nothing_chosen'];
  const all = document.getElementById('allon');
  if (all) all.checked = rows.length > 0 && rows.every(row => countOf(row) > 0);
}

for (const row of rows) {
  row.querySelector('.want').addEventListener('change', event => {
    setCount(row, event.target.checked ? 1 : 0);
  });
  row.querySelectorAll('button[data-step]').forEach(button => {
    button.addEventListener('click', () => setCount(row, countOf(row) + +button.dataset.step));
  });
}

const all = document.getElementById('allon');
if (all) all.addEventListener('change', () => {
  // Read once: setCount refreshes the header box, so reading it inside the
  // loop would turn the first row off again for every row after it.
  const wanted = all.checked ? 1 : 0;
  for (const row of rows) setCount(row, wanted);
});

const fillsheet = document.getElementById('fillsheet');
if (fillsheet) fillsheet.addEventListener('click', () => {
  // Spread a whole sheet over what is ticked, the remainder onto the first.
  const chosen = rows.filter(row => countOf(row) > 0);
  const spread = chosen.length ? chosen : rows;
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

// The text side. The server already decided which lines fit, so this only
// has to draw them: a preview that re-decided would promise what the sheet
// does not deliver.
const backdata = document.getElementById('backdata');
if (backdata) {
  const backs = JSON.parse(backdata.textContent);
  const disc = document.getElementById('backdisc');
  const who = document.getElementById('backwho');
  const wantback = document.getElementById('wantback');
  const wanttracks = document.getElementById('wanttracks');
  // The circle is drawn at this many pixels for a sticker of this many
  // millimetres, so a point on screen covers what it covers on paper.
  const DISC_PX = 240;
  const PX_PER_PT = DISC_PX / (data.diameterMm / 25.4 * 72);

  function drawBack() {
    const row = rows.find(row => countOf(row) > 0) || rows[0];
    const model = row && row.querySelector('input[name=models]').value;
    const back = model && backs[model];
    if (!back) { disc.style.display = 'none'; return; }

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
  drawBack();
  showBack();
}

for (const row of rows) setCount(row, countOf(row));
refresh();
