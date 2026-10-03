// Global settings.
(() => {
  const script = document.currentScript;
  const form = document.querySelector('form[action="/ui/settings"]');
  if (!form) return;
  const field = name => form.elements.namedItem(name);
  const changed = el => el.dispatchEvent(new Event('input', { bubbles: true }));  // unsaved.js: not saved yet

  // "Check model" and "Send test mail" try the values in the form without saving them. The button posts the
  // form to its formaction and asks for JSON; the answer shows below it and the form stays as it is,
  // passwords typed in too. Without JavaScript the button posts the form and the page comes back.
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
        method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' } });
      if (r.ok) answer = await r.json();
    } catch { /* offline or the server is gone: the message above */ }
    const banner = document.createElement('div');
    banner.className = `banner ${answer.tone}`;
    banner.textContent = answer.text;
    out.replaceChildren(banner);
    button.disabled = false;
    button.removeAttribute('aria-busy');
    label.textContent = idle;
    const bad = answer.field && field(answer.field);  // a value to fix: mark it and go there
    if (bad) {
      bad.setAttribute('aria-invalid', 'true');
      bad.focus();
    }
  });

  // a field marked as wrong (by the server or a test) is no longer marked once it is changed
  form.addEventListener('input', e => {
    if (e.target.getAttribute('aria-invalid') !== 'true') return;
    e.target.removeAttribute('aria-invalid');
    const message = document.getElementById(e.target.getAttribute('aria-describedby') || '');
    if (message) message.hidden = true;
  });

  // the port follows the encryption, unless it was set to something else than the usual one
  const ports = { starttls: '587', ssl: '465', none: '25' };
  const security = field('smtp_security'), port = field('smtp_port');
  if (security && port) {
    let before = security.value;
    security.addEventListener('change', () => {
      if (!port.value || port.value === ports[before]) {
        port.value = ports[security.value];
        changed(port);
      }
      before = security.value;
    });
  }

  // "Use http://…": the address this page was opened with, offered while the field is empty
  const url = field('ui_url'), use = document.querySelector('[data-use-url]');
  if (url && use) {
    const sync = () => { use.hidden = url.value.trim() !== ''; };
    sync();
    url.addEventListener('input', sync);
    use.querySelector('button').addEventListener('click', e => {
      url.value = e.currentTarget.dataset.value;
      changed(url);
      url.focus();
    });
  }
})();
