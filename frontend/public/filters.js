// Selection submits the complete GET form; the server owns results and counts.
for (const form of document.querySelectorAll('form[data-auto-submit]')) {
  form.addEventListener('change', event => {
    if (event.target instanceof HTMLSelectElement) form.requestSubmit();
  });
  // Keep the ordinary submit control usable if scripting is unavailable.
  const fallback = form.querySelector('[data-filter-submit]');
  if (fallback) fallback.hidden = true;
}
