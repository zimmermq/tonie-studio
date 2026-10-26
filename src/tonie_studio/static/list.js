// Search, filter, order and view for the list of a page: the tonies, the
// library. The page sends every row, so nothing is asked of teddyCloud again.
//
// What the page provides:
// - the list, marked data-list, its rows each with data-search (lower case
//   text the search looks through), data-tags (what the filter chips match)
//   and data-name, plus one data value per order, and optionally
//   data-language (what the language chips match);
// - #search, #sort, #count, #nohits, chips with data-filter ("" for all),
//   optionally chips with data-language ("" for all), which narrow what the
//   filter chips keep; a chip's span holds its number,
//   column headings with data-sort, and optionally buttons with data-view;
// - in #page-data: "orders", each order's {number, desc}, "first", the order
//   the list starts in, and the words "list.shown_of".
//
// The choice is kept in the address, so the back button and a bookmark bring
// it back; the view is also remembered in this browser, under the list's
// data-remember, as the way this person likes to look.
{
  const list = document.querySelector('[data-list]');
  const rows = list ? [...list.querySelectorAll('[data-search]')] : [];
  if (rows.length) {
    const data = pageData();
    const ORDERS = data.orders;
    const shownOf = data.words['list.shown_of'];
    const search = document.getElementById('search');
    const sort = document.getElementById('sort');
    const count = document.getElementById('count');
    const nohits = document.getElementById('nohits');
    const chips = [...document.querySelectorAll('[data-filter]')];
    const languages = [...document.querySelectorAll('button[data-language]')];
    const sorters = [...document.querySelectorAll('[data-sort]')];
    const views = [...document.querySelectorAll('button[data-view]')];
    const viewKey = list.dataset.remember;

    // Storage can be switched off or full; the page works without it.
    const remembered = () => { try { return localStorage.getItem(viewKey); } catch { return null; } };
    const remember = view => { try { localStorage.setItem(viewKey, view); } catch { /* not kept */ } };

    // Named rather than taken as the first of "orders": the page's JSON comes
    // with its keys in alphabetical order.
    const firstKey = ORDERS[data.first] ? data.first : Object.keys(ORDERS)[0];
    const asked = new URLSearchParams(location.search || '');
    const state = {
      q: asked.get('q') || '',
      // "kind" is what the tonie list called its filter before it had a name of its own.
      filter: asked.get('filter') || asked.get('kind') || '',
      language: asked.get('lang') || '',
      sort: ORDERS[asked.get('sort')] ? asked.get('sort') : firstKey,
      view: asked.get('view') || (views.length && remembered()) || list.dataset.view || '',
    };
    const firstWay = key => ORDERS[key].desc ? 'desc' : 'asc';
    state.dir = asked.get('dir') || firstWay(state.sort);

    // One collator for every comparison; localeCompare makes one per call.
    const collate = new Intl.Collator().compare;
    // A row without the value goes last either way round; rows alike go by name.
    function compare(a, b) {
      const left = a.dataset[state.sort], right = b.dataset[state.sort];
      if (!left !== !right) return left ? -1 : 1;
      const way = state.dir === 'desc' ? -1 : 1;
      const by = !left ? 0
        : ORDERS[state.sort].number ? Number(left) - Number(right) : collate(left, right);
      return way * by || collate(a.dataset.name, b.dataset.name);
    }

    // The list's own keys in the address; anything else there belongs to the
    // page, such as the listening page's period, and is kept as it is.
    function keep() {
      const params = new URLSearchParams(location.search || '');
      const put = (key, value, wanted) => {
        if (wanted) params.set(key, value); else params.delete(key);
      };
      params.delete('kind');
      put('q', state.q, state.q);
      put('filter', state.filter, state.filter);
      put('lang', state.language, state.language);
      put('sort', state.sort, state.sort !== firstKey);
      put('dir', state.dir, state.dir !== firstWay(state.sort));
      put('view', state.view, views.length && state.view !== list.dataset.view);
      const query = params.toString();
      try { history.replaceState(null, '', query ? `?${query}` : location.pathname); } catch { /* no history */ }
    }

    function filter() {
      const terms = searchWords(state.q);
      let shown = 0;
      for (const row of rows) {
        const tagged = !state.filter || row.dataset.tags.split(' ').includes(state.filter);
        const spoken = !state.language || row.dataset.language === state.language;
        row.hidden = !(tagged && spoken && matches(row, terms));
        if (!row.hidden) shown += 1;
      }
      count.textContent = shown === rows.length ? '' : say(shownOf, { shown, count: rows.length });
      number(terms);
      nohits.hidden = shown > 0;
      // As an attribute: the property reflecting it is missing in older browsers.
      for (const chip of chips) chip.setAttribute('aria-pressed', chip.dataset.filter === state.filter);
      for (const chip of languages) chip.setAttribute('aria-pressed', chip.dataset.language === state.language);
    }

    // Each chip's number is what a click on it would show: the search counts,
    // and so does the chip chosen in the other row, so a chip never promises
    // rows the search has already taken away.
    function number(terms) {
      const byTag = {}, byLanguage = {};
      let tagged = 0, spoken = 0;
      for (const row of rows) {
        if (!matches(row, terms)) continue;
        const tags = row.dataset.tags.split(' ');
        if (!state.language || row.dataset.language === state.language) {
          tagged += 1;
          for (const tag of tags) byTag[tag] = (byTag[tag] || 0) + 1;
        }
        if (!state.filter || tags.includes(state.filter)) {
          spoken += 1;
          byLanguage[row.dataset.language] = (byLanguage[row.dataset.language] || 0) + 1;
        }
      }
      const show = (chip, value) => {
        const span = chip.querySelector('span');
        if (span) span.textContent = String(value);
      };
      for (const chip of chips) show(chip, chip.dataset.filter ? byTag[chip.dataset.filter] || 0 : tagged);
      for (const chip of languages) {
        show(chip, chip.dataset.language ? byLanguage[chip.dataset.language] || 0 : spoken);
      }
    }

    // Appending moves a row, so the rows end up in order behind the column
    // heads. All in one go, and not at all when they are in order already, as
    // they usually are on arrival: 460 rows moved one by one is a long relayout.
    let current = rows;
    function order() {
      const sorted = [...rows].sort(compare);
      if (sorted.some((row, index) => row !== current[index])) {
        list.append(...sorted);
        current = sorted;
      }
      sort.value = state.sort;
      for (const sorter of sorters) {
        sorter.dataset.dir = sorter.dataset.sort === state.sort ? state.dir : '';
      }
    }

    function lay() {
      if (!views.length) return;
      list.dataset.view = state.view;
      for (const button of views) button.setAttribute('aria-pressed', button.dataset.view === state.view);
    }

    search.value = state.q;
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
      chip.addEventListener('click', () => { state.filter = chip.dataset.filter; filter(); keep(); });
    }
    for (const chip of languages) {
      chip.addEventListener('click', () => { state.language = chip.dataset.language; filter(); keep(); });
    }
    for (const button of views) {
      button.addEventListener('click', () => {
        state.view = button.dataset.view;
        remember(state.view);
        lay(); keep();
      });
    }
  }
}
