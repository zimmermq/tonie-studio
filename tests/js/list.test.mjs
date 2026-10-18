// The shared list of a page: search, filter, order and view, over the rows the server sent.
// The rows here are tonies, as the tonie list sends them.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

// As the page writes them: a value nobody knows is empty.
function tonie(name, { kind = 'official', picture = 'yes', added = 0, chapters = 1, series = '',
                       age = '', category = '' } = {}) {
  const tags = picture === 'no' ? `${kind} nopicture` : kind;
  return element('', '', { dataset: { name, kind, tags, added: added ? String(added) : '',
                                      chapters: chapters ? String(chapters) : '',
                                      series: `${series} ${name}`, age: String(age), category,
                                      search: `${series} ${name}`.toLowerCase() } });
}

async function page(rows) {
  const order = [];
  const list = element('tonies', '', { dataset: { view: 'grid', remember: 'tonies.view' } });
  list.querySelectorAll = () => rows;
  list.append = row => {
    if (order.includes(row)) order.splice(order.indexOf(row), 1);
    order.push(row);
  };
  const sort = element('sort');
  sort.value = 'name';
  const search = element('search');
  search.value = '';
  const chips = ['', 'official', 'own', 'nopicture'].map(kind => element('', '', { dataset: { filter: kind } }));
  const views = ['grid', 'list'].map(view => element('', '', { dataset: { view } }));
  const sorters = ['name', 'age', 'added'].map(key => element('', '', { dataset: { sort: key } }));
  const elements = await runPage(['app.js', 'list.js'], {
    ids: ['count', 'nohits'],
    prepared: { sort, search },
    found: { '[data-list]': list },
    pageData: { words: { 'list.shown_of': '{shown} of {count}' }, first: 'name',
                orders: { added: { number: true, desc: true }, name: {}, series: {}, kind: {}, category: {}, age: { number: true },
                          chapters: { number: true, desc: true } } },
    queries: { '[data-filter]': chips, '[data-view]': views, '[data-sort]': sorters },
  });
  const names = () => order.filter(row => !row.hidden).map(row => row.dataset.name);
  const chip = kind => chips.find(c => c.dataset.filter === kind);
  const view = name => views.find(v => v.dataset.view === name);
  const sorter = key => sorters.find(s => s.dataset.sort === key);
  return { elements, list, sort, search, names, chip, view, sorter };
}

// The search keeps what has every word, and says how many it shows.
{
  const { search, names, elements } = await page([
    tonie('madagascar'), tonie('simba', { series: 'Disney' }), tonie('olaf', { series: 'Disney' })]);
  search.value = 'disney simba';
  search.fire('input');
  assert.deepEqual(names(), ['simba']);
  assert.equal(elements.count.textContent, '1 of 3');
  assert.equal(elements.nohits.hidden, true);
}

// A filter keeps one kind; "no picture" goes by the picture, not the kind.
{
  const { chip, names } = await page([
    tonie('a', { kind: 'own' }), tonie('b'), tonie('c', { kind: 'own', picture: 'no' })]);
  chip('own').fire('click');
  assert.deepEqual(names(), ['a', 'c']);
  chip('nopicture').fire('click');
  assert.deepEqual(names(), ['c']);
  assert.equal(chip('nopicture').ariaPressed, 'true');
  assert.equal(chip('own').ariaPressed, 'false');
}

// Newest first, the undated last; equal ones by name.
{
  const { sort, names } = await page([
    tonie('b', { added: 0 }), tonie('a', { added: 200 }), tonie('c', { added: 900 }), tonie('d', { added: 200 })]);
  sort.value = 'added';
  sort.fire('change');
  assert.deepEqual(names(), ['c', 'a', 'd', 'b']);
}

// Most chapters first.
{
  const { sort, names } = await page([tonie('a', { chapters: 3 }), tonie('b', { chapters: 20 })]);
  sort.value = 'chapters';
  sort.fire('change');
  assert.deepEqual(names(), ['b', 'a']);
}

// The view switch changes how the list is laid out.
{
  const { view, list } = await page([tonie('a')]);
  view('list').fire('click');
  assert.equal(list.dataset.view, 'list');
  assert.equal(view('list').ariaPressed, 'true');
}

// Nothing found says so.
{
  const { search, elements } = await page([tonie('a')]);
  search.value = 'zzz';
  search.fire('input');
  assert.equal(elements.nohits.hidden, false);
}

// A column heading orders by it; clicked again, the other way round.
{
  const { sorter, names, sort } = await page([
    tonie('b', { age: 6 }), tonie('a', { age: 3 }), tonie('c'), tonie('d', { age: 3 })]);
  sorter('age').fire('click');
  assert.deepEqual(names(), ['a', 'd', 'b', 'c']);
  assert.equal(sorter('age').dataset.dir, 'asc');
  assert.equal(sort.value, 'age');
  sorter('age').fire('click');
  // Oldest first now; the tonie without an age still comes last.
  assert.deepEqual(names(), ['b', 'a', 'd', 'c']);
  assert.equal(sorter('age').dataset.dir, 'desc');
  assert.equal(sorter('name').dataset.dir, '');
}

// Another heading starts its own way round: dates newest first.
{
  const { sorter, names } = await page([tonie('a', { added: 100 }), tonie('b', { added: 900 })]);
  sorter('age').fire('click');
  sorter('added').fire('click');
  assert.deepEqual(names(), ['b', 'a']);
}

// The list starts in the order the page names, not the first one it lists.
{
  const { names } = await page([tonie('b', { added: 900 }), tonie('a', { added: 100 })]);
  assert.deepEqual(names(), ['a', 'b']);
}
