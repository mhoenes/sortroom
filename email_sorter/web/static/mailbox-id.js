// Add mailbox: live preview of the new mailbox's folder name (its id), derived from the display name.
// Mirrors config.mailbox_id_for; the server decides, this only shows what it will do.
(() => {
  const idFor = (name, taken) => {
    let s = name.toLowerCase();
    for (const [a, b] of [['ä', 'ae'], ['ö', 'oe'], ['ü', 'ue'], ['ß', 'ss']]) s = s.split(a).join(b);
    s = s.normalize('NFKD').replace(/[^\x00-\x7f]/g, '');
    s = s.replace(/[^a-z0-9_]+/g, '-').replace(/-{2,}/g, '-').replace(/^[-_]+|[-_]+$/g, '');
    const base = s.slice(0, 40).replace(/[-_]+$/, '') || 'mailbox';
    let candidate = base;
    for (let n = 2; taken.includes(candidate); n++) {
      candidate = base.slice(0, 39 - String(n).length).replace(/[-_]+$/, '') + '-' + n;
    }
    return candidate;
  };
  document.querySelectorAll('input[data-id-preview]').forEach(input => {
    const out = document.getElementById(input.dataset.idPreview);
    const note = input.dataset.idNote ? document.getElementById(input.dataset.idNote) : null;
    const taken = JSON.parse(input.dataset.taken || '[]');
    const current = input.dataset.current || '';
    const show = () => {
      const id = idFor(input.value.trim(), taken);
      out.textContent = id;
      if (note) note.hidden = !current || id === current;
    };
    input.addEventListener('input', show);
    show();
  });
  // Add mailbox: with no name yet, the login address is the name until a name is typed (the user can change it)
  const name = document.querySelector('input[name=name][data-id-preview]');
  const user = document.querySelector('input[name=imap_user]');
  if (name && user && !name.value) {
    let typed = false;
    name.addEventListener('input', e => { if (e.isTrusted) typed = true; });  // not our own input event below
    user.addEventListener('input', () => {
      if (typed) return;
      name.value = user.value.trim().slice(0, name.maxLength > 0 ? name.maxLength : 60);
      name.dispatchEvent(new Event('input', { bubbles: true }));  // the folder name follows
    });
  }
  // the settings page: the folder name is set by hand; say what changing it does
  document.querySelectorAll('input[data-rename-note]').forEach(input => {
    const note = document.getElementById(input.dataset.renameNote);
    const show = () => { note.hidden = input.value.trim() === input.dataset.current; };
    input.addEventListener('input', show);
    show();
    // shown as text with "Rename" until it is to be changed – unless it was changed already or is wrong
    const view = input.parentElement.querySelector('.rename-view');
    if (!view || input.value !== input.dataset.current || input.getAttribute('aria-invalid')) return;
    input.hidden = true;
    view.hidden = false;
    const open = view.querySelector('button');
    if (open) open.addEventListener('click', () => {
      view.hidden = true;
      input.hidden = false;
      input.focus();
      input.select();
    });
  });
})();
