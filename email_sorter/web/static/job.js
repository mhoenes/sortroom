// The job page: while the job runs, fetch its state and the new log lines instead of reloading the page (so the
// scroll position and a selection stay), and reload once it is over to show the result. Also the two tools of the
// log: show only warnings and errors, and copy it.
(() => {
  const data = document.currentScript.dataset;
  const log = document.getElementById('job-log'), empty = document.getElementById('log-empty');
  const last = document.getElementById('job-last');
  const tools = document.querySelector('.log-tools');
  tools.hidden = log.hidden;  // nothing to filter or copy yet
  document.getElementById('log-problems').addEventListener('change', e => log.classList.toggle('only-problems', e.target.checked));
  const copy = document.getElementById('log-copy');
  copy.addEventListener('click', async () => {
    const idle = copy.textContent;
    try { await navigator.clipboard.writeText([...log.children].map(l => l.textContent).join('\n')); } catch { return; }
    copy.textContent = data.copied;
    setTimeout(() => { copy.textContent = idle; }, 2000);
  });

  if (data.running !== '1') return;
  let total = Number(data.total), failures = 0;
  const add = lines => {
    const following = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;  // at the bottom: keep following
    for (const line of lines) {
      const span = document.createElement('span');
      span.className = `l ${line.level}`;
      span.textContent = line.text;
      log.append(span);
    }
    if (!lines.length) return;
    log.hidden = false;
    tools.hidden = false;
    empty.hidden = true;
    if (last) last.textContent = lines[lines.length - 1].text;
    if (following) log.scrollTop = log.scrollHeight;
  };
  const poll = async () => {
    try {
      const r = await fetch(`${data.url}?after=${total}`, { headers: { Accept: 'application/json' } });
      if (r.ok) {
        const state = await r.json();
        add(state.lines);
        total = state.total;
        failures = 0;
        if (state.status !== 'running') { location.reload(); return; }  // over: show the result
      } else { failures++; }
    } catch { failures++; }
    if (failures > 5) { if (last) last.textContent = data.lost; return; }
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 1000);
})();
