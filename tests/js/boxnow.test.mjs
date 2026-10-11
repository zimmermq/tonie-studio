// The box line in the header: why it says teddyCloud cannot be followed.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

const words = JSON.stringify({ 'box.unreachable': 'unreachable', 'box.silent': 'silent',
                               'box.playing': 'playing', 'box.on_box': 'on the box' });

async function header(box) {
  const page = await runPage(['app.js'], {
    ids: ['boxnow'], texts: { 'box-words': words }, answers: { '/api/box': box },
    // The hover preview every page has; the header code is set up beside it.
    prepared: { peek: element('peek', '', { children: { img: element('') } }) },
  });
  // The header is filled once the page is read, like every script part that needs the page.
  await page.dispatch('DOMContentLoaded');
  return page.boxnow.textContent;
}

assert.equal(await header({ connected: false, silent: true }), 'silent');
assert.equal(await header({ connected: false, silent: false }), 'unreachable');
console.log('boxnow.test: ok');
