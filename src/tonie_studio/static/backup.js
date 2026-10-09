// The script of the backup page; see templates/backup.html.
const data = JSON.parse(document.getElementById('page-data').textContent);

const words = data.words;
const picker = document.getElementById('restorefile');
picker.addEventListener('change', () => {
  document.getElementById('restorepicked').textContent =
    picker.files.length ? picker.files[0].name : words['common.nothing_chosen'];
});
