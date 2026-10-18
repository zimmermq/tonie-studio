// The script of the tonies page; see templates/tonies.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

// The marker follows the box live: teddyCloud pushes the events, this only
// asks for the result. Polling every few seconds is plenty for something a
// person changes by hand.
const note = document.getElementById('boxnote');
const words = data.words;

document.addEventListener('box', event => {
  const box = event.detail;
  for (const marker of document.querySelectorAll('.onbox')) marker.hidden = true;
  const row = box.ruid ? document.getElementById(`tag-${box.ruid}`) : null;

  if (box.on_box && row) {
    row.querySelector('.onbox').hidden = false;
    note.textContent = box.playing ? words['tonies.note_playing'] : words['tonies.note_on_box'];
  } else if (!box.connected) {
    note.textContent = words['tonies.note_offline'];
  } else if (box.stale) {
    note.textContent = words['tonies.note_stale'];
  } else {
    note.textContent = words['tonies.note_nothing'];
  }
});
