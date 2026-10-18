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
  const sorters = [...document.querySelectorAll('[data-sort]')];
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

  // Each order and the way it first runs: dates and chapters newest and
  // most first, everything else from the start of the alphabet or the
  // youngest age. A row without the value goes last either way round, and
  // rows alike go by name.
  const ORDERS = {
    name: { number: false, desc: false },
    series: { number: false, desc: false },
    kind: { number: false, desc: false },
    category: { number: false, desc: false },
    age: { number: true, desc: false },
    added: { number: true, desc: true },
    chapters: { number: true, desc: true },
  };
  const firstWay = key => (ORDERS[key] || ORDERS.name).desc ? 'desc' : 'asc';
  state.dir = asked.get('dir') || firstWay(state.sort);

  function compare(a, b) {
    const key = ORDERS[state.sort] ? state.sort : 'name';
    const left = a.dataset[key], right = b.dataset[key];
    if (!left !== !right) return left ? -1 : 1;
    const way = state.dir === 'desc' ? -1 : 1;
    const by = !left ? 0
      : ORDERS[key].number ? Number(left) - Number(right) : left.localeCompare(right);
    return way * by || a.dataset.name.localeCompare(b.dataset.name);
  }

  function keep() {
    const params = new URLSearchParams();
    if (state.q) params.set('q', state.q);
    if (state.kind) params.set('kind', state.kind);
    if (state.sort !== 'name') params.set('sort', state.sort);
    if (state.dir !== firstWay(state.sort)) params.set('dir', state.dir);
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
    for (const row of [...rows].sort(compare)) list.append(row);
    sort.value = state.sort;
    for (const sorter of sorters) {
      sorter.dataset.dir = sorter.dataset.sort === state.sort ? state.dir : '';
    }
  }

  function lay() {
    list.dataset.view = state.view;
    for (const button of views) button.ariaPressed = String(button.dataset.view === state.view);
  }

  search.value = state.q;
  sort.value = state.sort;
  filter(); order(); lay();

  search.addEventListener('input', () => { state.q = search.value; filter(); keep(); });
  sort.addEventListener('change', () => {
    state.sort = sort.value;
    state.dir = firstWay(state.sort);
    order(); keep();
  });
  for (const sorter of sorters) {
    sorter.addEventListener('click', () => {
      const again = sorter.dataset.sort === state.sort;
      state.sort = sorter.dataset.sort;
      state.dir = !again ? firstWay(state.sort) : state.dir === 'asc' ? 'desc' : 'asc';
      order(); keep();
    });
  }
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
