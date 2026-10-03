// Global settings: "Check model" and "Send test mail" try the values in the form without saving them. The
// button posts the form to its formaction and asks for JSON; the answer shows below it and the form stays as
// it is, passwords typed in too. Without JavaScript the button posts the form and the page comes back.
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
    button.disabled = false;
    button.removeAttribute('aria-busy');
    label.textContent = idle;
  });
})();
