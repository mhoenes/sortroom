// Show the fields of the chosen sign-in method (data-auth="password", "oauth", "google" or "microsoft")
// and, until the method is picked by hand, suggest it from the IMAP server (mirrors oauth.provider_for_host).
(() => {
  const guess = host => {
    const h = host.trim().toLowerCase().replace(/\.$/, '');
    if (/(^|\.)(gmail|googlemail)\.com$/.test(h)) return 'google';
    if (/(^|\.)(office365|outlook|hotmail|live)\.com$/.test(h)) return 'microsoft';
    return 'password';
  };
  document.querySelectorAll('select[data-auth-select]').forEach(select => {
    const scope = select.closest('[data-auth-scope]') || document;
    const show = () => {
      scope.querySelectorAll('[data-auth]').forEach(el => {
        const wanted = el.dataset.auth.split(' ');
        el.hidden = !(wanted.includes(select.value) || (wanted.includes('oauth') && select.value !== 'password'));
      });
    };
    select.addEventListener('change', () => { select.dataset.picked = '1'; show(); });
    const host = select.form && select.form.querySelector('input[name=imap_host]');
    if (host && select.dataset.suggest !== undefined) {
      host.addEventListener('input', () => {
        if (!select.dataset.picked) { select.value = guess(host.value); show(); }
      });
    }
    show();
  });
})();
