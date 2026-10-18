// The tonie list's marker for what lies on the box, following the box live.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

const WORDS = { 'tonies.note_playing': ', playing', 'tonies.note_on_box': ', on the box',
                'tonies.note_offline': ', offline', 'tonies.note_stale': ', stale',
                'tonies.note_nothing': ', nothing' };

async function page() {
  const marker = element('');
  marker.hidden = true;
  const row = element('tag-11dfc517500304e0', '', { children: { '.onbox': marker } });
  const elements = await runPage(['app.js', 'tonies.js'], {
    ids: ['boxnote'],
    prepared: { 'tag-11dfc517500304e0': row },
    queries: { '.onbox': [marker] },
    pageData: { words: WORDS },
  });
  return { elements, marker };
}

// The tonie on the box is marked, and the note says whether it plays.
{
  const { elements, marker, } = await page();
  await elements.dispatch('box', { ruid: '11dfc517500304e0', on_box: true, playing: true,
                                   connected: true });
  assert.equal(marker.hidden, false);
  assert.equal(elements.boxnote.textContent, ', playing');
}

// Lifted, the mark goes; without teddyCloud the note says so.
{
  const { elements, marker } = await page();
  await elements.dispatch('box', { ruid: '11dfc517500304e0', on_box: true, connected: true });
  await elements.dispatch('box', { ruid: null, on_box: false, connected: false });
  assert.equal(marker.hidden, true);
  assert.equal(elements.boxnote.textContent, ', offline');
}
