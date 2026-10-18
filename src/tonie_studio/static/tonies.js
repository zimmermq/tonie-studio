// The script of the tonies page; see templates/tonies.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

// The marker follows the box live: teddyCloud pushes the events, this only
// asks for the result. Polling every few seconds is plenty for something a
// person changes by hand.
const note = document.getElementById('boxnote');
const words = data.words;

document.addEventListener('box', event => {
  const box = event.detail;
  for (const marker of document.querySelectorAll('.onbox')) marker.hidden = true;
  const row = box.ruid ? document.getElementById(`tag-${box.ruid}`) : null;

  if (box.on_box && row) {
    row.querySelector('.onbox').hidden = false;
    note.textContent = box.playing ? words['tonies.note_playing'] : words['tonies.note_on_box'];
  } else if (!box.connected) {
    note.textContent = words['tonies.note_offline'];
  } else if (box.stale) {
    note.textContent = words['tonies.note_stale'];
  } else {
    note.textContent = words['tonies.note_nothing'];
  }
});

// Search, filter, order and view work on the rows the server sent, all of
// them, so nothing is asked of teddyCloud again. The choice is kept in the
// address, so the back button and a bookmark bring it back; the view is also
// remembered in this browser, as the way this person likes to look.
const list = document.getElementById('tonies');
const rows = list ? [...list.querySelectorAll('.tonie')] : [];
if (rows.length) {
  const search = document.getElementById('search');
  const sort = document.getElementById('sort');
  const count = document.getElementById('count');
  const nohits = document.getElementById('nohits');
  const chips = [...document.querySelectorAll('[data-kind-filter]')];
  const views = [...document.querySelectorAll('[data-view]')];
  const VIEW_KEY = 'tonies.view';

  // Storage can be switched off or full; the page works without it.
  const remembered = () => { try { return localStorage.getItem(VIEW_KEY); } catch { return null; } };
  const remember = view => { try { localStorage.setItem(VIEW_KEY, view); } catch { /* not kept */ } };

  const asked = new URLSearchParams(location.search || '');
  const state = {
    q: asked.get('q') || '',
    kind: asked.get('kind') || '',
    sort: asked.get('sort') || 'name',
    view: asked.get('view') || remembered() || 'grid',
  };

  const byName = (a, b) => a.dataset.name.localeCompare(b.dataset.name);
  // Bigger first for numbers; a row without one (0) goes last. Ties by name.
  const byNumber = field => (a, b) => Number(b.dataset[field]) - Number(a.dataset[field]) || byName(a, b);
  const ORDERS = {
    name: byName,
    series: (a, b) => a.dataset.series.localeCompare(b.dataset.series),
    added: byNumber('added'),
    chapters: byNumber('chapters'),
  };

  function keep() {
    const params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.kind) params.set('kind', state.kind);
    if (state.sort !== 'name') params.set('sort', state.sort);
    if (state.view !== 'grid') params.set('view', state.view);
    const query = params.toString();
    try { history.replaceState(null, '', query ? `?${query}` : location.pathname); } catch { /* no history */ }
  }

  function filter() {
    const terms = state.q.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const row of rows) {
      const kind = state.kind === 'nopicture' ? row.dataset.picture === 'no'
        : !state.kind || row.dataset.kind === state.kind;
      row.hidden = !(kind && terms.every(term => row.dataset.search.includes(term)));
      if (!row.hidden) shown += 1;
    }
    count.textContent = shown === rows.length ? ''
      : say(words['tonies.shown_of'], { shown, count: rows.length });
    nohits.hidden = shown > 0;
    for (const chip of chips) chip.ariaPressed = String(chip.dataset.kindFilter === state.kind);
  }

  // Appending moves a row, so the rows end up in order behind the column heads.
  function order() {
    for (const row of [...rows].sort(ORDERS[state.sort] || byName)) list.append(row);
  }

  function lay() {
    list.dataset.view = state.view;
    for (const button of views) button.ariaPressed = String(button.dataset.view === state.view);
  }

  search.value = state.q;
  sort.value = state.sort;
  filter(); order(); lay();

  search.addEventListener('input', () => { state.q = search.value; filter(); keep(); });
  sort.addEventListener('change', () => { state.sort = sort.value; order(); keep(); });
  for (const chip of chips) {
    chip.addEventListener('click', () => { state.kind = chip.dataset.kindFilter; filter(); keep(); });
  }
  for (const button of views) {
    button.addEventListener('click', () => {
      state.view = button.dataset.view;
      remember(state.view);
      lay(); keep();
    });
  }
}
