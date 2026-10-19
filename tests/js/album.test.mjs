// The album page: leaving it after changing the cover's crop asks first, the
// same as after changing titles, whether by "Cancel" or by the language.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

function input(id, value) {
  return Object.assign(element(id), { value, min: '0.5', max: '2.5' });
}

async function page() {
  const tracks = Object.assign(element('tracks'), { children: [] });
  const circle = Object.assign(element('cover'), { clientWidth: 200, setPointerCapture() {} });
  const picks = [element('', '', { dataset: { index: '0' } }), element('', '', { dataset: { index: '1' } })];
  const language = element('');
  const asked = [];
  const album = await runPage(['app.js', 'album.js'], {
    ids: ['coverimg', 'coverbg', 'coverreset', 'chosen', 'partshint', 'form', 'order', 'cancel'],
    prepared: { cover: circle, tracks, zoom: input('zoom', '1.5'), cx: input('cx', '0'),
                cy: input('cy', '0'), cover_index: input('cover_index', '0'),
                parts: input('parts', '1') },
    queries: { '.pick': picks, '.languages a': [language] },
    pageData: { id: 'a1', mostChapters: 99, words: { 'album.discard': 'discard?', 'album.chosen': '' } },
    globals: { confirm: question => { asked.push(question); return false; } },
  });
  // Whether leaving by ``link`` was stopped, after asking.
  const leaving = link => {
    let stopped = false;
    link.fire('click', { preventDefault() { stopped = true; } });
    return stopped;
  };
  return { album, circle, picks, language, asked, leaving };
}

// Untouched, the page is left without a question.
{
  const { album, asked, leaving } = await page();
  assert.equal(leaving(album.cancel), false);
  assert.deepEqual(asked, []);
}

// Dragging the picture is a change.
{
  const { album, circle, leaving } = await page();
  circle.fire('pointerdown', { pointerId: 1, clientX: 100, clientY: 100 });
  circle.fire('pointermove', { pointerId: 1, clientX: 140, clientY: 100 });
  assert.notEqual(album.cx.value, '0');
  assert.equal(leaving(album.cancel), true);
}

// So is a pinch.
{
  const { album, circle, leaving } = await page();
  circle.fire('pointerdown', { pointerId: 1, clientX: 50, clientY: 100 });
  circle.fire('pointerdown', { pointerId: 2, clientX: 150, clientY: 100 });
  circle.fire('pointermove', { pointerId: 2, clientX: 150, clientY: 100 });
  circle.fire('pointermove', { pointerId: 2, clientX: 190, clientY: 100 });
  for (const id of [1, 2]) circle.fire('pointerup', { pointerId: id });
  assert.notEqual(album.zoom.value, '1.5');
  assert.equal(leaving(album.cancel), true);
}

// So is resetting the crop.
{
  const { album, leaving } = await page();
  album.coverreset.fire('click');
  assert.equal(leaving(album.cancel), true);
}

// So is choosing another picture.
{
  const { album, picks, leaving } = await page();
  picks[1].fire('click');
  assert.equal(album.cover_index.value, '1');
  assert.equal(leaving(album.cancel), true);
}

// Switching the language reloads the page as well, and asks the same.
{
  const { album, language, asked, leaving } = await page();
  assert.equal(leaving(language), false);
  album.form.fire('input');
  assert.equal(leaving(language), true);
  assert.deepEqual(asked, ['discard?']);
}
console.log('album.test: ok');
