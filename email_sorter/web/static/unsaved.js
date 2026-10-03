// Unsaved changes. A form marked data-guard="<what it holds>" remembers that something was changed and shows
// its .unsaved hint. Leaving the page then asks first, and so does saving another form on the same page:
// Settings has several forms, and each one saves only its own fields.
(() => {
  document.documentElement.classList.add('js');  // app.css: the save bar's button
  const script = document.currentScript;
  const dirty = new Set();
  let leaving = false;
  const markForm = form => {
    dirty.add(form);
    form.classList.add('dirty');  // its save button stands out now (app.css)
    form.querySelectorAll('.unsaved').forEach(el => { el.hidden = false; });
  };
  const mark = e => {
    const form = e.target.closest && e.target.closest('form[data-guard]');
    if (form) markForm(form);
  };
  // a page that came back with what was entered (an error, a test without JavaScript): not saved either
  document.querySelectorAll('form[data-guard][data-dirty]').forEach(markForm);
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
