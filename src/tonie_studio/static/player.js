// The player at the foot of every page. One audio element plays what a
// [data-play] button names (see the play macro in _parts.html): a library taf
// as Ogg audio, with its chapters to skip between.
//
// Every link loads a new page, which ends the audio. So what plays, and
// where, is kept for the tab and offered again on the next page, at the same
// place; a browser that does not let audio start without a click leaves it
// paused there.

// Where each chapter starts is a list of seconds, the first 0. A chapter
// counts as reached half a second early: seeking lands a hair before the
// time asked for.
window.chapters = (() => {
  // Back to the start of the chapter rather than to the one before, once
  // this many seconds of it have played, as music players do.
  const RESTART_AFTER = 3;

  function chapterAt(starts, time) {
    let index = 0;
    starts.forEach((start, at) => { if (start <= time + 0.5) index = at; });
    return index;
  }

  function previousStart(starts, time) {
    if (!starts.length) return 0;
    const index = chapterAt(starts, time);
    return index === 0 || time - starts[index] > RESTART_AFTER ? starts[index] : starts[index - 1];
  }

  // Null after the last chapter.
  function nextStart(starts, time) {
    const index = chapterAt(starts, time);
    return index + 1 < starts.length ? starts[index + 1] : null;
  }

  return { chapterAt, previousStart, nextStart };
})();

(() => {
  const bar = document.getElementById('player');
  if (!bar) return;
  const audio = document.getElementById('player-audio');
  const title = document.getElementById('player-title');
  const detail = document.getElementById('player-detail');
  const chapter = document.getElementById('player-chapter');
  const picture = document.getElementById('player-picture');
  const previous = document.getElementById('player-previous');
  const next = document.getElementById('player-next');
  const words = JSON.parse(document.getElementById('player-words').textContent);

  // What plays, kept for the tab; the volume, kept for every visit.
  const KEPT = 'player';
  const VOLUME = 'playerVolume';

  // Storage can be missing or refuse, in a private window or with site data
  // blocked; the player then only forgets more.
  function area(name) {
    try {
      return window[name] || null;
    } catch (error) {
      return null;
    }
  }
  function recall(name, key) {
    try {
      return JSON.parse(area(name)?.getItem(key) ?? 'null');
    } catch (error) {
      return null;
    }
  }
  function keep(name, key, value) {
    try {
      if (value === null) area(name)?.removeItem(key);
      else area(name)?.setItem(key, JSON.stringify(value));
    } catch (error) {
      // Forgotten, then.
    }
  }

  // What plays now: the dataset of the button that started it, read once.
  let now = null;

  function fromButton(button) {
    const data = button.dataset;
    let tracks = [];
    try {
      tracks = JSON.parse(data.tracks || '[]');
    } catch (error) {
      tracks = [];
    }
    return {
      play: data.play, title: data.title || '', subtitle: data.subtitle || '',
      picture: data.picture || '', tracks,
      starts: (data.starts || '').split(',').filter(Boolean).map(Number),
    };
  }

  // Older Safari plays no Ogg Opus, the only form teddyCloud hands a taf out in.
  function canPlay() {
    const answer = audio.canPlayType('audio/ogg; codecs=opus');
    return answer === 'probably' || answer === 'maybe';
  }

  function describe() {
    if (!now) return;
    const starts = now.starts;
    if (starts.length > 1) {
      const index = window.chapters.chapterAt(starts, audio.currentTime || 0);
      chapter.textContent = window.say(words['player.chapter'], {
        number: index + 1, count: starts.length, title: now.tracks[index] || '' }).trim();
    } else {
      chapter.textContent = '';
    }
    previous.disabled = next.disabled = starts.length < 2;
  }

  // Which buttons on the page name what plays, and whether it plays.
  function mark() {
    const playing = now && !audio.paused;
    for (const button of document.querySelectorAll('[data-play]')) {
      const on = Boolean(now) && button.dataset.play === now.play;
      button.classList.toggle('playing', on && playing);
      button.setAttribute('aria-pressed', String(on && playing));
    }
  }

  function announce() {
    if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: now.title, artist: now.subtitle,
        artwork: now.picture ? [{ src: now.picture }] : [] });
      navigator.mediaSession.setActionHandler('previoustrack', toPrevious);
      navigator.mediaSession.setActionHandler('nexttrack', now.starts.length > 1 ? toNext : null);
    } catch (error) {
      // Media keys are a nicety; the player works without them.
    }
  }

  // Shows ``item`` in the bar and loads it; false when this browser cannot play it.
  function show(item) {
    now = item;
    bar.hidden = false;
    document.body?.classList.add('with-player');
    title.textContent = item.title;
    detail.textContent = item.subtitle;
    picture.hidden = !item.picture;
    if (item.picture) picture.setAttribute('src', item.picture);
    if (!canPlay()) {
      detail.textContent = words['player.no_ogg'];
      chapter.textContent = '';
      return false;
    }
    if (audio.getAttribute('src') !== item.play) {
      audio.setAttribute('src', item.play);
      audio.load();
    }
    describe();
    announce();
    return true;
  }

  function start() {
    const playing = audio.play();
    if (playing && playing.catch) playing.catch(() => mark());
  }

  function press(button) {
    const item = fromButton(button);
    if (now && now.play === item.play && audio.getAttribute('src') === item.play) {
      if (audio.paused) start(); else audio.pause();
      return;
    }
    if (show(item)) start();
  }

  function toPrevious() {
    if (now) audio.currentTime = window.chapters.previousStart(now.starts, audio.currentTime);
  }

  function toNext() {
    if (!now) return;
    const at = window.chapters.nextStart(now.starts, audio.currentTime);
    if (at !== null) audio.currentTime = at;
  }

  function close() {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    now = null;
    bar.hidden = true;
    document.body?.classList.remove('with-player');
    keep('sessionStorage', KEPT, null);
    mark();
  }

  // A row that is a link carries a span that acts as a button: a click on it
  // plays and does not follow the link.
  document.addEventListener('click', event => {
    const button = event.target?.closest?.('[data-play]');
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    press(button);
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const button = event.target?.closest?.('span[data-play]');
    if (!button) return;
    event.preventDefault();
    press(button);
  });

  previous.addEventListener('click', toPrevious);
  next.addEventListener('click', toNext);
  document.getElementById('player-close').addEventListener('click', close);
  for (const type of ['play', 'pause', 'ended']) audio.addEventListener(type, mark);
  audio.addEventListener('timeupdate', describe);
  audio.addEventListener('volumechange', () => keep('localStorage', VOLUME, audio.volume));
  window.addEventListener('pagehide', () => {
    if (now) keep('sessionStorage', KEPT, { ...now, time: audio.currentTime || 0, playing: !audio.paused });
  });

  const volume = recall('localStorage', VOLUME);
  if (typeof volume === 'number' && volume >= 0 && volume <= 1) audio.volume = volume;

  // What the last page played, back where it was.
  const kept = recall('sessionStorage', KEPT);
  if (kept && kept.play && show({ ...kept, starts: kept.starts || [], tracks: kept.tracks || [] })) {
    audio.addEventListener('loadedmetadata', () => {
      audio.currentTime = kept.time || 0;
      if (kept.playing) start();
    }, { once: true });
  }
  mark();
})();
