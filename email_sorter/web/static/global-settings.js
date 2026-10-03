// Global settings: the port follows the encryption, and the address of the interface can be taken from
// the page. The test buttons are test-button.js.
(() => {
  const form = document.querySelector('form[action="/ui/settings"]');
  if (!form) return;
  const field = name => form.elements.namedItem(name);
  const changed = el => el.dispatchEvent(new Event('input', { bubbles: true }));  // unsaved.js: not saved yet

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
