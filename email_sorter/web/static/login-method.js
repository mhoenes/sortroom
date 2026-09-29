// Show the fields of the chosen sign-in method: elements with data-auth="password", "oauth", "google"
// or "microsoft". The method comes from a <select data-auth-select> (the settings page) or from a
// group of radio buttons with data-auth-value (the mailbox type on "Add mailbox"). A select with
// data-suggest also follows the IMAP server until it is picked by hand (mirrors oauth.provider_for_host).
(() => {
  const guess = host => {
    const h = host.trim().toLowerCase().replace(/\.$/, '');
    if (/(^|\.)(gmail|googlemail)\.com$/.test(h)) return 'google';
    if (/(^|\.)(office365|outlook|hotmail|live)\.com$/.test(h)) return 'microsoft';
    return 'password';
  };
  document.querySelectorAll('[data-auth-select]').forEach(control => {
    const scope = control.closest('[data-auth-scope]') || document;
    const method = () => {
      if (control.tagName === 'SELECT') return control.value;
      const checked = control.querySelector('input:checked');
      return checked ? checked.dataset.authValue : '';
    };
    const show = () => {
      const value = method();
      scope.querySelectorAll('[data-auth]').forEach(el => {
        const wanted = el.dataset.auth.split(' ');
        el.hidden = !value || !(wanted.includes(value) || (wanted.includes('oauth') && value !== 'password'));
      });
      // Microsoft: an empty client ID means Sortroom's own app
      scope.querySelectorAll('[data-ms-placeholder]').forEach(el => {
        el.placeholder = value === 'microsoft' ? el.dataset.msPlaceholder : '';
      });
    };
    control.addEventListener('change', () => { control.dataset.picked = '1'; show(); });
    const host = control.tagName === 'SELECT' && control.dataset.suggest !== undefined
      && control.form && control.form.querySelector('input[name=imap_host]');
    if (host) {
      host.addEventListener('input', () => {
        if (!control.dataset.picked) { control.value = guess(host.value); show(); }
      });
    }
    show();
  });
})();
