// Show or hide the password on the login page: a button at the end of the field, added here because it
// does nothing without the script. The text of its label comes from the script tag's data attributes.
(() => {
  const script = document.currentScript;
  const field = document.querySelector('.pw input');
  if (!field) return;
  const eye = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/>';
  const icons = {
    show: eye + '<circle cx="12" cy="12" r="3"/></svg>',
    hide: eye + '<path d="M4 4l16 16"/></svg>',
  };
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'icon-btn';
  const set = shown => {
    field.type = shown ? 'text' : 'password';
    button.innerHTML = shown ? icons.hide : icons.show;
    button.setAttribute('aria-label', shown ? script.dataset.hide : script.dataset.show);
    button.title = button.getAttribute('aria-label');
    button.setAttribute('aria-pressed', String(shown));
  };
  button.addEventListener('click', () => { set(field.type === 'password'); field.focus(); });
  set(false);
  field.parentElement.append(button);
})();
