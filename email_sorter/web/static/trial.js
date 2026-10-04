// The result page of a category test while it runs: fetch how far it is instead of reloading the page, and reload
// once it is over to show the result (or why it failed).
(() => {
  const data = document.currentScript.dataset;
  const bar = document.getElementById('trial-progress'), count = document.getElementById('trial-count');
  let failures = 0;
  const poll = async () => {
    try {
      const r = await fetch(data.url, { headers: { Accept: 'application/json' } });
      if (r.ok) {
        const state = await r.json();
        if (state.status !== 'running') { location.reload(); return; }
        bar.max = state.total || 1;
        bar.value = state.done;
        count.textContent = state.progress;
        failures = 0;
      } else { failures++; }
    } catch { failures++; }
    if (failures > 5) { count.textContent = data.lost; return; }
    setTimeout(poll, 1500);
  };
  setTimeout(poll, 800);
})();
