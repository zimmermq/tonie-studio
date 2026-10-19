// The assign page: a sticker held against the box while it is open is
// offered, and what was typed on the page survives that.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

const words = { 'assign.new_tag': 'new', 'assign.unknown_tag': 'unknown',
                'assign.unknown_tag_hint': 'again' };

function field(name, value) {
  return Object.assign(element(''), { name, value });
}

// ``listed`` are the stickers the page was drawn with.
async function page(listed, { search = '' } = {}) {
  const radios = listed.map(ruid => Object.assign(element(''), { value: ruid }));
  return runPage(['app.js', 'assign.js'], {
    ids: ['hint'],
    pageData: { words },
    queries: { 'input[name=ruid]': radios },
    found: { '[name=series]': field('series', 'Lichterkinder'),
             '[name=episode]': field('episode', 'Halloween & Co'),
             '[name=category]': field('category', 'audio-play') },
    globals: { location: { pathname: '/job/j1/assign', search } },
  });
}

const target = assigned => new URLSearchParams(String(assigned.window.location).split('?')[1]);

// teddyCloud files a fresh sticker on its first read, so the box reports it on
// the box rather than as unknown; the page still has to offer it.
{
  const assigned = await page(['aaaa']);
  await assigned.dispatch('box', { ruid: 'bbbb', on_box: true, unknown_ruid: null });
  assert.equal(target(assigned).get('new'), 'bbbb');
  assert.equal(assigned.hint.textContent, 'new');
}

// A sticker teddyCloud does not know is offered as before.
{
  const assigned = await page(['aaaa']);
  await assigned.dispatch('box', { ruid: 'cccc', on_box: true, unknown_ruid: 'cccc' });
  assert.equal(target(assigned).get('new'), 'cccc');
}

// The name, series and category typed so far go along with the new sticker.
{
  const assigned = await page(['aaaa']);
  await assigned.dispatch('box', { ruid: 'bbbb', on_box: true, unknown_ruid: null });
  const params = target(assigned);
  assert.equal(params.get('series'), 'Lichterkinder');
  assert.equal(params.get('episode'), 'Halloween & Co');
  assert.equal(params.get('category'), 'audio-play');
  assert.ok(String(assigned.window.location).startsWith('/job/j1/assign?'));
}

// A sticker already in the list, or one taken off again, changes nothing.
{
  const assigned = await page(['aaaa']);
  await assigned.dispatch('box', { ruid: 'aaaa', on_box: true, unknown_ruid: null });
  await assigned.dispatch('box', { ruid: 'dddd', on_box: false, unknown_ruid: null });
  assert.equal(typeof assigned.window.location, 'object');
}

// Reloaded for a sticker once and still without it: no second reload.
{
  const assigned = await page(['aaaa'], { search: '?new=bbbb&series=x' });
  assigned.document.createElement = () => element('');
  assigned.hint.replaceChildren = (...parts) => {
    assigned.hint.textContent = parts.map(part => part.textContent ?? part).join('');
  };
  await assigned.dispatch('box', { ruid: 'bbbb', on_box: true, unknown_ruid: null });
  assert.equal(typeof assigned.window.location, 'object');
  assert.equal(assigned.hint.textContent, 'unknown bbbb again');
}
console.log('assign.test: ok');
