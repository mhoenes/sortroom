// Test buttons (Check model, Send test mail, Check connection): they try the values in their form without
// saving them. The button posts the form to its formaction and asks for JSON; the answer shows below it and
// the form stays as it is, passwords typed in too. Without JavaScript the button posts the form and the page
// comes back with the answer. See _test.html.
(() => {
  const script = document.currentScript;
  document.addEventListener('click', async e => {
    const button = e.target.closest('button[data-test]');
    if (!button || button.disabled) return;
    e.preventDefault();  // no submit: the form stays, and unsaved.js still counts it as not saved
    const out = document.querySelector(`[data-result="${button.dataset.test}"]`);
    const label = button.querySelector('span');
    const idle = label.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    label.textContent = script.dataset.busy;
    out.replaceChildren();
    let answer = { tone: 'err', text: script.dataset.failed };
    try {
      const r = await fetch(button.formAction, {
        method: 'POST', body: new FormData(button.form), headers: { Accept: 'application/json' } });
      if (r.ok) answer = await r.json();
    } catch { /* offline or the server is gone: the message above */ }
    const banner = document.createElement('div');
    banner.className = `banner ${answer.tone}`;
    banner.textContent = answer.text;
    out.replaceChildren(banner);
    if (answer.details) {  // e.g. the target folders the connection test found
      const details = document.createElement('pre');
      details.className = 'test-details';
      details.textContent = answer.details;
      out.append(details);
    }
    button.disabled = false;
    button.removeAttribute('aria-busy');
    label.textContent = idle;
    const bad = answer.field && button.form.elements.namedItem(answer.field);  // a value to fix: go there
    if (bad && bad.focus) {
      bad.setAttribute('aria-invalid', 'true');
      bad.focus();
    }
  });

  // a field marked as wrong (by the server or a test) is no longer marked once it is changed
  document.addEventListener('input', e => {
    if (!e.target.getAttribute || e.target.getAttribute('aria-invalid') !== 'true') return;
    e.target.removeAttribute('aria-invalid');
    const message = document.getElementById(e.target.getAttribute('aria-describedby') || '');
    if (message) message.hidden = true;
  });
})();
