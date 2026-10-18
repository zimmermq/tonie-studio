// What every page shares. Loaded in the head, so the helpers below exist
// before a page's own script runs; whatever needs the page itself waits for
// it to be read.

// A text from the catalogs, filled in the way the server fills it: {name}
// placeholders, and a {one, other} pair chosen by count. Pages get their
// texts handed over as JSON and say them through this.
window.say = (text, values = {}) => {
  if (text && typeof text === 'object') text = values.count === 1 ? text.one : text.other;
  return String(text ?? '').replace(/\{(\w+)\}/g,
    (match, name) => (name in values ? values[name] : match));
};

// Calls ``step`` now and again every ``ms`` for as long as it returns true.
// A step that fails - a dropped connection, a server restarting - is tried
// again rather than ending the loop: the work being watched goes on.
window.every = (ms, step) => {
  const run = async () => {
    let again = true;
    try {
      again = await step();
    } catch (error) {
      again = true;
    }
    if (again) setTimeout(run, ms);
  };
  run();
};

// Under a file picker, what it holds: ``describe`` turns the chosen files into
// a line, and with none the label shows again what the server put there.
// Without it, choosing a folder of sixty files looks like nothing happened.
window.showPicked = (input, slot, describe) => {
  const empty = slot.textContent;
  input.addEventListener('change', () => {
    slot.textContent = input.files.length ? describe([...input.files]) : empty;
  });
};

// A form that cannot be taken back carries its question in data-confirm,
// already in the reader's language; it is sent only once that is answered.
// A choice inside the form can carry one too, asked only when it is chosen:
// an occupied sticker among free ones. So can the button pressed, where one
// form has two ways to be sent; both questions are then asked as one.
document.addEventListener('submit', event => {
  const form = event.target;
  const chosen = form.querySelector('input:checked[data-confirm], option:checked[data-confirm]');
  const pressed = event.submitter && event.submitter.dataset.confirm;
  const question = form.dataset.confirm
    || [pressed, chosen && chosen.dataset.confirm].filter(Boolean).join('\n\n');
  if (question && !window.confirm(question)) event.preventDefault();
});

// One menu open at a time, and a click anywhere else closes it: a details
// element on its own stays open until its own summary is clicked again.
document.addEventListener('click', event => {
  for (const menu of document.querySelectorAll('details.menu[open]')) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});

document.addEventListener('DOMContentLoaded', () => {
  // A 44 pixel thumbnail is enough to find a row and too small to judge a
  // cover by, which is exactly what one does before printing it.
  (() => {
    const peek = document.getElementById('peek');
    const image = peek.querySelector('img');
    if (!window.matchMedia('(hover: hover)').matches) return;

    function bigger(src) {
      // The album covers take a size; the model covers are served whole.
      return src.includes('size=') ? src.replace(/size=\d+/, 'size=520') : src;
    }

    // Every cover in the application is served from a path ending in /cover,
    // either the archive's or a model's. Recognising the picture rather than
    // the page it sits on means a new page gets this without knowing about it.
    const COVER_URL = /\/cover[/?]/;

    // Two shapes carry a cover: an image with a src, and the sheet's circles,
    // which are drawn from the row's own cover and have no image element.
    function coverOf(target) {
      const thumb = target.closest('img');
      if (thumb && COVER_URL.test(thumb.getAttribute('src') || '')) {
        return { anchor: thumb, src: thumb.src, round: false };
      }
      // The circle only, not the whole row: ticking a row is done with the
      // pointer on it, and a picture jumping out at every tick is noise.
      const circle = target.closest('.list .album[data-src] .preview');
      if (!circle) return null;
      // Each circle stands for one way of meeting the rim, so the large
      // picture has to be that one - pointing at "ganzes Cover" and being
      // shown the edge-to-edge version answers a question nobody asked.
      const mode = circle.closest('.mode');
      const chosen = mode && mode.querySelector('input[type=radio]');
      const src = circle.closest('[data-src]').dataset.src;
      return {
        anchor: circle,
        src: chosen ? `${src}?mode=${encodeURIComponent(chosen.value)}&size=520` : src,
        // A sticker is cut round, and the whole point of the choice is what
        // the rim does to the artwork.
        round: true,
      };
    }

    document.addEventListener('mouseover', event => {
      const cover = coverOf(event.target);
      if (!cover) return;
      image.src = bigger(cover.src);
      image.style.borderRadius = peek.style.borderRadius = cover.round ? '50%' : '';
      peek.style.display = 'block';
      place(cover.anchor);
    });

    document.addEventListener('mouseout', event => {
      if (coverOf(event.target)) peek.style.display = 'none';
    });

    function place(anchor) {
      // Never over the picture being pointed at, which is the one thing the
      // preview exists to show. Beside the column when its margin is wide
      // enough, otherwise against whichever window edge is further from the
      // picture, accepting that it covers something else on the way.
      const box = anchor.getBoundingClientRect();
      const column = document.querySelector('main').getBoundingClientRect();
      const gapRight = window.innerWidth - column.right - 24;
      const gapLeft = column.left - 24;
      const beside = Math.max(gapRight, gapLeft);
      const size = Math.min(300, Math.max(beside, window.innerWidth / 3));
      if (size < 120) {  // a preview this small says nothing a thumbnail does not
        peek.style.display = 'none';
        return;
      }

      let left;
      if (beside >= size) {
        left = gapRight >= gapLeft ? column.right + 16 : column.left - size - 16;
      } else {
        const toTheRight = box.left + box.width / 2 < window.innerWidth / 2;
        left = toTheRight ? window.innerWidth - size - 16 : 16;
      }

      image.style.width = image.style.height = `${size}px`;
      peek.style.left = `${left}px`;
      peek.style.top = `${Math.max(12, Math.min(box.top + box.height / 2 - size / 2,
                                                window.innerHeight - size - 12))}px`;
      peek.style.display = 'block';
    }

    window.addEventListener('scroll', () => { peek.style.display = 'none'; }, true);
  })();

  // What is on the box belongs on every page, not only on the tonie list: it
  // is the one piece of state that changes without anyone touching this app.
  // This is the only place that asks; every answer goes out as a "box" event
  // on the document, and a page that cares about the box listens for that
  // rather than asking again.
  (() => {
    const slot = document.getElementById('boxnow');
    const words = JSON.parse(document.getElementById('box-words').textContent);
    let known = {};

    async function tick() {
      try {
        const response = await fetch('/api/box');
        if (response.ok) {
          known = await response.json();
          document.dispatchEvent(new CustomEvent('box', {detail: known}));
        }
      } catch (error) {
        // A dropped poll says nothing about the box.
      }
      if (slot) render();
      setTimeout(tick, 3000);
    }

    function render() {
      slot.classList.toggle('off', known.connected === false);
      if (known.connected === false) {
        slot.hidden = false;
        slot.textContent = words[known.silent ? 'box.silent' : 'box.unreachable'];
        return;
      }
      if (known.on_box && known.label) {
        slot.hidden = false;
        slot.textContent = `${known.playing ? words['box.playing'] : words['box.on_box']} `;
        slot.appendChild(document.createElement('b'));
        slot.querySelector('b').textContent = known.label;
        return;
      }
      slot.hidden = true;
    }
    tick();
  })();
});
