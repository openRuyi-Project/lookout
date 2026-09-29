function revealAnchor() {
  let id;
  try { id = decodeURIComponent(location.hash.slice(1)); }
  catch { return; }
  const target = id && document.getElementById(id);
  if (!target) return;
  let details = target.closest('details');
  while (details) {
    details.open = true;
    details = details.parentElement?.closest('details') ?? null;
  }
  target.scrollIntoView({block: 'start'});
}

revealAnchor();
window.addEventListener('hashchange', revealAnchor);
