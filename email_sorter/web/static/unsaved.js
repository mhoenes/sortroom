// Unsaved changes. A form marked data-guard="<what it holds>" remembers that something was changed and shows
// its .unsaved hint. Leaving the page then asks first, and so does saving another form on the same page:
// Settings has several forms, and each one saves only its own fields.
(() => {
  const script = document.currentScript;
  const dirty = new Set();
  let leaving = false;
  const mark = e => {
    const form = e.target.closest && e.target.closest('form[data-guard]');
    if (!form) return;
    dirty.add(form);
    form.querySelectorAll('.unsaved').forEach(el => { el.hidden = false; });
  };
  document.addEventListener('input', mark);
  document.addEventListener('change', mark);
  document.addEventListener('submit', e => {
    const others = [...dirty].filter(form => form !== e.target);
    if (others.length && !confirm(script.dataset.other.replace('%s', others.map(f => f.dataset.guard).join(', ')))) {
      e.preventDefault();
      return;
    }
    leaving = true;
  });
  addEventListener('beforeunload', e => {
    if (!dirty.size || leaving) return;
    e.preventDefault();  // the browser asks with its own text
    e.returnValue = '';  // older Safari and Chrome
  });
  addEventListener('pageshow', () => { leaving = false; });  // back from the browser's cache
})();
