// What a file button holds, said beside it on every page by the shared handler.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

const words = element('common-words', JSON.stringify({ 'common.files': { one: '{count} file', other: '{count} files' } }));

async function choose(files) {
  const slot = element('slot', 'nothing chosen');
  const input = Object.assign(element(''), { files, dataset: { picked: 'slot' } });
  const page = await runPage(['app.js'], { prepared: { slot, 'common-words': words } });
  await page.dispatch('change', undefined, { target: input });
  return { page, slot, input };
}

// One file is named, several are counted.
{
  const { slot } = await choose([{ name: 'cover.png' }]);
  assert.equal(slot.textContent, 'cover.png');
}
{
  const { slot } = await choose([{ name: 'a.nfc' }, { name: 'b.nfc' }, { name: 'c.nfc' }]);
  assert.equal(slot.textContent, '3 files');
}

// Choosing nothing again brings back what the server put there.
{
  const { page, slot, input } = await choose([{ name: 'cover.png' }]);
  input.files = [];
  await page.dispatch('change', undefined, { target: input });
  assert.equal(slot.textContent, 'nothing chosen');
}
