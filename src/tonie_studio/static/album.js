// The script of the album page; see templates/album.html.
const data = pageData();

// Whether leaving the page would throw work away; see the end of this file.
// The crop is set from script, which fires no input event, so everything
// that moves it says so itself.
let changed = false;

// The preview mirrors what the server will render: the cover scaled inside
// the circle and shifted, with the same arithmetic on both sides. A CSS
// background position of 0 percent is the window the server cuts at
// offset -1, so what is seen here is what ends up on the sticker.
const coverimg = document.getElementById('coverimg');
if (coverimg) {
  const circle = document.getElementById('cover');
  const coverbg = document.getElementById('coverbg');
  const zoom = document.getElementById('zoom');
  const cx = document.getElementById('cx');
  const cy = document.getElementById('cy');
  const index = document.getElementById('cover_index');
  const clamp = value => Math.max(-1, Math.min(1, value));

  function paint() {
    const z = +zoom.value;
    const url = `url("/album/${data.id}/cover?index=${index.value}&size=512")`;
    coverimg.style.backgroundImage = url;
    coverbg.style.backgroundImage = url;
    coverimg.style.backgroundSize = `${z * 100}% ${z * 100}%`;
    coverimg.style.backgroundPosition =
      `${(1 + +cx.value) * 50}% ${(1 + +cy.value) * 50}%`;
  }

  // Dragging moves the picture under the circle. At zoom 1 there is nothing
  // to move, so the whole gesture is a no-op rather than a jitter.
  let dragging = null;
  circle.addEventListener('pointerdown', event => {
    if (+zoom.value <= 1) return;
    dragging = { x: event.clientX, y: event.clientY, cx: +cx.value, cy: +cy.value };
    circle.setPointerCapture(event.pointerId);
  });
  circle.addEventListener('pointermove', event => {
    if (!dragging) return;
    const z = +zoom.value;
    // Across the whole circle the offset runs from -1 to 1, and the reachable
    // range shrinks as the zoom grows.
    const span = circle.clientWidth * (z - 1) / z / 2;
    cx.value = clamp(dragging.cx - (event.clientX - dragging.x) / span).toFixed(3);
    cy.value = clamp(dragging.cy - (event.clientY - dragging.y) / span).toFixed(3);
    changed = true;
    paint();
  });
  for (const done of ['pointerup', 'pointercancel']) {
    circle.addEventListener(done, () => { dragging = null; });
  }

  // No wheel handler on purpose. Zooming on the wheel means swallowing it,
  // and the circle sits near the top of a page taller than any window: a
  // pointer resting over it would leave the page unable to scroll at all.
  // The slider zooms on a desktop, two fingers on a touch screen.

  // Pinch: two pointers on the circle, the distance between them is the zoom.
  const touches = new Map();
  let pinch = null;
  circle.addEventListener('pointerdown', event => touches.set(event.pointerId, event));
  circle.addEventListener('pointermove', event => {
    if (!touches.has(event.pointerId)) return;
    touches.set(event.pointerId, event);
    if (touches.size !== 2) return;
    dragging = null;
    const [a, b] = [...touches.values()];
    const gap = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    if (pinch === null) { pinch = { gap, zoom: +zoom.value }; return; }
    // The slider's own limits, so a pinch reaches exactly what the slider does.
    zoom.value = Math.max(+zoom.min, Math.min(+zoom.max, pinch.zoom * gap / pinch.gap));
    changed = true;
    paint();
  });
  for (const done of ['pointerup', 'pointercancel', 'pointerleave']) {
    circle.addEventListener(done, event => { touches.delete(event.pointerId); pinch = null; });
  }

  zoom.addEventListener('input', paint);
  document.getElementById('coverreset').addEventListener('click', () => {
    zoom.value = 1; cx.value = 0; cy.value = 0; changed = true; paint();
  });
  document.querySelectorAll('.pick').forEach(pick => {
    pick.addEventListener('click', () => {
      document.querySelectorAll('.pick').forEach(other => {
        other.classList.remove('chosen');
        other.setAttribute('aria-pressed', 'false');
      });
      pick.classList.add('chosen');
      pick.setAttribute('aria-pressed', 'true');
      index.value = pick.dataset.index;
      changed = true;
      paint();
    });
  });
  paint();
}

// Reordering happens in the DOM; the hidden field is rewritten from it on
// submit. Without scripting the field keeps the album's own order, so the
// form still works.
const list = document.getElementById('tracks');

list.addEventListener('click', event => {
  const button = event.target.closest('button[data-move]');
  if (!button) return;
  const item = button.closest('li');
  const sibling = button.dataset.move === 'up'
    ? item.previousElementSibling : item.nextElementSibling;
  if (!sibling) return;
  // The neighbour is the one that moves: the row holding the pressed button
  // stays in the page, so the keyboard stays on that button.
  button.dataset.move === 'up'
    ? list.insertBefore(sibling, item.nextElementSibling) : list.insertBefore(sibling, item);
  renumber();
});

// Karaoke takes arrive unticked: half of these albums are a second pass of
// their own first half, and nobody wants both on one tonie.
const boxes = () => [...list.querySelectorAll('input[type=checkbox]')];
const chosen = document.getElementById('chosen');

// A taf can be skipped through by at most this many chapters; teddyCloud
// writes anything beyond into the last one without a word.
const mostChapters = data.mostChapters;
const words = data.words;
const partsField = document.getElementById('parts');
const partsHint = document.getElementById('partshint');

function recount() {
  const picked = [...list.children].filter(item => item.querySelector('input[type=checkbox]').checked);
  const n = picked.length;
  chosen.textContent = say(words['album.chosen'], {chosen: n, count: boxes().length});

  const needed = Math.max(1, Math.ceil(n / mostChapters));
  partsField.min = needed;
  if (+partsField.value < needed) partsField.value = needed;
  const parts = Math.max(1, +partsField.value || 1);
  const seconds = picked.reduce((sum, item) => sum + (+item.dataset.seconds || 0), 0);
  const each = seconds && parts ? Math.round(seconds / parts / 60) : 0;

  partsHint.textContent = parts === 1
    ? (needed > 1 ? '' : words['album.parts_one'])
    : say(each ? words['album.parts_many_minutes'] : words['album.parts_many'], {count: parts, minutes: each})
      + (needed > 1 ? ' ' + say(words['album.parts_limit'], {count: mostChapters}) : '');
}
partsField.addEventListener('input', recount);

document.querySelectorAll('button[data-pick]').forEach(button => {
  button.addEventListener('click', () => {
    for (const item of list.children) {
      const box = item.querySelector('input[type=checkbox]');
      box.checked = button.dataset.pick === 'all'
        || (button.dataset.pick === 'main' && item.dataset.extra === 'no');
    }
    recount();
  });
});

list.addEventListener('change', recount);
recount();

// The number and every label that names it follow a row to its new place.
function renumber() {
  const words = pageData().words;
  [...list.children].forEach((item, index) => {
    const number = index + 1;
    item.querySelector('span').textContent = number;
    item.querySelector('input[type=checkbox]').setAttribute(
      'aria-label', say(words['album.track_use'], {number}));
    item.querySelector('input[type=text]').setAttribute(
      'aria-label', say(words['album.track_title'], {number}));
    item.querySelector('[data-move=up]').setAttribute('aria-label', say(words['album.move_up'], {number}));
    item.querySelector('[data-move=down]').setAttribute(
      'aria-label', say(words['album.move_down'], {number}));
  });
}

// The titles are submitted in the order the rows sit in, which is the order
// this field records, so the nth title belongs to the nth number.
document.getElementById('form').addEventListener('submit', () => {
  document.getElementById('order').value =
    [...list.children].map(item => item.dataset.number).join(',');
});

// Leaving by "Cancel" after changing titles, order or the crop throws all of
// it away; asked first, as every other page asks before losing work. Another
// language draws the page again from the album itself, so it loses the same.
const form = document.getElementById('form');
form.addEventListener('input', () => { changed = true; });
form.addEventListener('change', () => { changed = true; });
list.addEventListener('click', event => { if (event.target.closest('[data-move]')) changed = true; });
const askBeforeLeaving = event => {
  if (changed && !window.confirm(words['album.discard'])) event.preventDefault();
};
document.getElementById('cancel').addEventListener('click', askBeforeLeaving);
document.querySelectorAll('.languages a').forEach(link => link.addEventListener('click', askBeforeLeaving));

