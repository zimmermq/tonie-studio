// The script of the batch page; see templates/batch.html.
// The stickers shown per row are only what the automatic way would do;
// placing them decides the order at the box instead.
const form = document.getElementById('batch');
const button = form.querySelector('button[type=submit]');
function showHow() {
  const how = form.querySelector('input[name=stickers]:checked').value;
  for (const cell of form.querySelectorAll('.sticker')) {
    cell.hidden = how === 'place' && !cell.querySelector('.state-failed');
  }
  for (const note of form.querySelectorAll('.only-auto')) note.hidden = how !== 'auto';
  if (!button.disabled) button.textContent = button.dataset[how];
}
form.addEventListener('change', showHow);
showHow();
