// The script of the tonies page; see templates/tonies.html.
const data = pageData();

// The marker follows the box live: teddyCloud pushes the events, this only
// asks for the result. Polling every few seconds is plenty for something a
// person changes by hand.
const note = document.getElementById('boxnote');
const words = data.words;

// Only the marker that was shown is hidden again: a report every few seconds
// that touched every row would rewrite 460 of them each time.
let marked = null;
document.addEventListener('box', event => {
  const box = event.detail;
  const row = box.ruid ? document.getElementById(`tag-${box.ruid}`) : null;
  const marker = box.on_box && row ? row.querySelector('.onbox') : null;
  if (marked !== marker) {
    if (marked) marked.hidden = true;
    if (marker) marker.hidden = false;
    marked = marker;
  }

  if (marker) {
    note.textContent = box.playing ? words['tonies.note_playing'] : words['tonies.note_on_box'];
  } else if (!box.connected) {
    note.textContent = words['tonies.note_offline'];
  } else if (box.stale) {
    note.textContent = words['tonies.note_stale'];
  } else {
    note.textContent = words['tonies.note_nothing'];
  }
});
