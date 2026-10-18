// The tonie list: search, filter, order and view, over the rows the server sent.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

function tonie(name, { kind = 'official', picture = 'yes', added = 0, chapters = 1, series = '' } = {}) {
  return element('', '', { dataset: { name, kind, picture, added: String(added),
                                      chapters: String(chapters), series: `${series} ${name}`,
                                      search: `${series} ${name}`.toLowerCase() } });
}

async function page(rows) {
  const order = [];
  const list = element('tonies', '', { dataset: { view: 'grid' } });
  list.querySelectorAll = () => rows;
  list.append = row => {
    if (order.includes(row)) order.splice(order.indexOf(row), 1);
    order.push(row);
  };
  const sort = element('sort');
  sort.value = 'name';
  const search = element('search');
  search.value = '';
  const chips = ['', 'official', 'own', 'nopicture'].map(kind => element('', '', { dataset: { kindFilter: kind } }));
  const views = ['grid', 'list'].map(view => element('', '', { dataset: { view } }));
  const elements = await runPage(['app.js', 'tonies.js'], {
    ids: ['boxnote', 'count', 'nohits'],
    prepared: { tonies: list, sort, search },
    pageData: { words: { 'tonies.shown_of': '{shown} of {count}' } },
    queries: { '[data-kind-filter]': chips, '[data-view]': views },
  });
  const names = () => order.filter(row => !row.hidden).map(row => row.dataset.name);
  const chip = kind => chips.find(c => c.dataset.kindFilter === kind);
  const view = name => views.find(v => v.dataset.view === name);
  return { elements, list, sort, search, names, chip, view };
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
