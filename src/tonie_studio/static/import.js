// While a run is active, the import page reloads itself whenever the run
// moves on. A run settles one tonie every few minutes, so a whole new page is
// cheap and keeps all the drawing in the template.
(() => {
  const data = pageData();
  if (!data.watch) return;
  let seen = null;
  window.every(5000, async () => {
    const response = await fetch('/api/import');
    if (!response.ok) return true;
    const state = await response.json();
    const now = JSON.stringify(state);
    if (seen !== null && now !== seen) {
      window.location.reload();
      return false;
    }
    seen = now;
    return true;
  });
})();
