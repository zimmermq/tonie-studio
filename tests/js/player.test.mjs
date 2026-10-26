// The player at the foot of every page: which chapter plays, skipping between
// them, and starting what a play button names.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

const WORDS = { 'player.chapter': '{number}/{count} {title}', 'player.no_ogg': 'No Ogg here' };

// An audio element as far as the player uses it; ``ogg`` is what
// canPlayType answers for Ogg Opus.
function audioElement(ogg = 'probably') {
  const audio = element('player-audio');
  const attributes = {};
  return Object.assign(audio, {
    paused: true, currentTime: 0, volume: 1, played: 0, loaded: 0,
    canPlayType: () => ogg,
    setAttribute(name, value) { attributes[name] = String(value); },
    getAttribute: name => (name in attributes ? attributes[name] : null),
    removeAttribute(name) { delete attributes[name]; },
    load() { this.loaded += 1; },
    play() { this.paused = false; this.played += 1; return Promise.resolve(); },
    pause() { this.paused = true; },
  });
}

function playButton() {
  return Object.assign(element('button', '', { dataset: {
    play: '/audio/by/audioID/7.taf', title: 'Ratatouille', subtitle: 'Disney',
    picture: '/cover/cache/7.png', starts: '0,95,230', tracks: '["Eins","Zwei","Drei"]' } }), {
    closest: () => null,
  });
}

async function playerPage(audio = audioElement()) {
  const ids = ['player', 'player-title', 'player-detail', 'player-chapter', 'player-picture',
               'player-previous', 'player-next', 'player-close'];
  const page = await runPage(['app.js', 'player.js'], {
    ids, prepared: { 'player-audio': audio }, texts: { 'player-words': JSON.stringify(WORDS) },
  });
  page.player.hidden = true;
  return page;
}

// A click on ``button``, as the document's listener is handed it.
// The page's dispatch hands its listeners a copy, so the flag lives outside it.
function clickOn(button) {
  const seen = { prevented: false };
  seen.event = { target: { closest: () => button },
                 preventDefault() { seen.prevented = true; }, stopPropagation() {} };
  return seen;
}

{
  // Which chapter plays, and where skipping goes.
  const { window } = await playerPage();
  const { chapterAt, previousStart, nextStart } = window.chapters;
  const starts = [0, 95, 230];
  assert.equal(chapterAt(starts, 0), 0);
  assert.equal(chapterAt(starts, 94.7), 1, 'seeking lands a hair early');
  assert.equal(chapterAt(starts, 400), 2);
  assert.equal(nextStart(starts, 10), 95);
  assert.equal(nextStart(starts, 300), null, 'nothing after the last chapter');
  assert.equal(previousStart(starts, 120), 95, 'well into a chapter: back to its start');
  assert.equal(previousStart(starts, 96), 0, 'at its start: the chapter before');
  assert.equal(previousStart(starts, 1), 0);
  assert.equal(previousStart([], 50), 0);
}

{
  // A play button starts its audio in the bar, and the link it sits in is not followed.
  const audio = audioElement();
  const page = await playerPage(audio);
  const event = clickOn(playButton());

  await page.dispatch('click', undefined, event.event);

  assert.equal(event.prevented, true);
  assert.equal(audio.getAttribute('src'), '/audio/by/audioID/7.taf');
  assert.equal(audio.played, 1);
  assert.equal(page.player.hidden, false);
  assert.equal(page['player-title'].textContent, 'Ratatouille');
  assert.equal(page['player-chapter'].textContent, '1/3 Eins');
  assert.equal(page['player-next'].disabled, false);

  audio.currentTime = 100;
  page['player-next'].fire('click');
  assert.equal(audio.currentTime, 230);
  page['player-previous'].fire('click');
  assert.equal(audio.currentTime, 95);
}

{
  // The same button again pauses, and once more plays on from there.
  const audio = audioElement();
  const page = await playerPage(audio);
  const button = playButton();

  await page.dispatch('click', undefined, clickOn(button).event);
  await page.dispatch('click', undefined, clickOn(button).event);
  assert.equal(audio.paused, true);
  await page.dispatch('click', undefined, clickOn(button).event);

  assert.equal(audio.paused, false);
  assert.equal(audio.loaded, 1, 'loaded once, not again for each press');
}

{
  // A browser without Ogg Opus is told so instead of failing silently.
  const audio = audioElement('');
  const page = await playerPage(audio);

  await page.dispatch('click', undefined, clickOn(playButton()).event);

  assert.equal(audio.played, 0);
  assert.equal(page['player-detail'].textContent, 'No Ogg here');
}

{
  // A click elsewhere is left alone.
  const audio = audioElement();
  const page = await playerPage(audio);
  const event = clickOn(null);

  await page.dispatch('click', undefined, event.event);

  assert.equal(event.prevented, false);
  assert.equal(page.player.hidden, true);
}
