import htmx, {type HtmxBeforeSwapDetails, type HtmxRequestConfig, type HtmxResponseInfo} from 'htmx.org';
import {revealAnchor} from './reveal-anchor';

Object.assign(htmx.config, {
  allowEval: false, allowScriptTags: false, selfRequestsOnly: true,
  includeIndicatorStyles: false, historyCacheSize: 0, historyRestoreAsHxRequest: false,
  scrollIntoViewOnBoost: false, allowNestedOobSwaps: false, timeout: 10000,
  // Settling an inline style attribute is blocked by our style-src 'self' CSP.
  attributesToSettle: ['class', 'width', 'height'],
});
htmx.config.responseHandling.unshift({code: '304', swap: false});

const interval = 30000;
const refresh = document.getElementById('page-refresh');
const historySource = document.getElementById('page-history');
let timer: ReturnType<typeof setTimeout>;
let failures = 0;
let validator: {url: string; etag: string} | undefined;
const restore = new WeakMap<XMLHttpRequest, () => void>();
const currentURL = () => location.pathname + location.search;
const background = (request: HtmxRequestConfig) => request.elt === refresh;
const historyPage = (request: HtmxRequestConfig) => request.elt.matches('a.entry-more');

function status(message = '') {
  const element = document.getElementById('page-status');
  if (element) { element.textContent = message; element.hidden = !message; }
}

function schedule(delay = Math.min(interval * 2 ** failures, 300000)) {
  clearTimeout(timer);
  if (document.hidden || !navigator.onLine) return;
  timer = setTimeout(() => {
    schedule();
    if (!interacting() && document.querySelector('#page[data-page-fingerprint]')) {
      htmx.trigger(refresh!, 'refresh');
    }
  }, delay);
}

function interacting() {
  return !!document.querySelector('.section-entries[data-expanded]')
    || !!document.activeElement?.closest('form')
    || !!document.querySelector('[popover]:popover-open')
    || window.getSelection()?.isCollapsed === false;
}

function assets(page: Document) {
  return [...page.querySelectorAll('script[src], head link[rel="stylesheet"]')]
    .map(element => element.getAttribute('src') || element.getAttribute('href')).sort().join('\n');
}

function preserveReadingState() {
  const x = scrollX, y = scrollY;
  const expanded = [...document.querySelectorAll('details[open][id]')].map(element => element.id);
  const horizontal = new Map([...document.querySelectorAll('.table-wrap')]
    .map(element => [element.getAttribute('aria-label'), element.scrollLeft]));
  const input = document.querySelector<HTMLInputElement>('#search');
  const draft = input && input.value !== input.defaultValue ? input.value : undefined;
  const active = document.activeElement;
  const focusID = active?.id;
  const focusHref = active?.getAttribute('href');
  return () => {
    for (const id of expanded) {
      const element = document.getElementById(id);
      if (element instanceof HTMLDetailsElement) element.open = true;
    }
    for (const element of document.querySelectorAll('.table-wrap')) {
      element.scrollLeft = horizontal.get(element.getAttribute('aria-label')) || 0;
    }
    const search = document.querySelector<HTMLInputElement>('#search');
    if (search && draft !== undefined) search.value = draft;
    const focused = focusID ? document.getElementById(focusID)
      : focusHref ? [...document.querySelectorAll<HTMLAnchorElement>('a[href]')]
        .find(element => element.getAttribute('href') === focusHref) : null;
    focused?.focus({preventScroll: true});
    window.scrollTo(x, y);
  };
}

document.addEventListener('htmx:configRequest', event => {
  const request = (event as CustomEvent<HtmxRequestConfig>).detail;
  // Every edit is a complete GET URL; repeating it in a header wastes the HTTP budget.
  delete request.headers['HX-Current-URL'];
  if (background(request) && validator?.url === currentURL()) {
    request.headers['If-None-Match'] = validator.etag;
  }
});

document.addEventListener('htmx:beforeSwap', event => {
  const result = (event as CustomEvent<HtmxBeforeSwapDetails>).detail;
  if (!result.shouldSwap) return;
  const page = new DOMParser().parseFromString(result.serverResponse, 'text/html');
  const incoming = page.querySelector<HTMLElement>('#page[data-page-fingerprint]');
  const responseURL = new URL(result.xhr.responseURL);
  if (!incoming || responseURL.origin !== location.origin) {
    result.shouldSwap = false;
    result.isError = true;
    return;
  }
  // Changed application assets require a new runtime, not execution of response scripts.
  if (assets(page) !== assets(document)) {
    result.shouldSwap = false;
    location.assign(result.pathInfo.finalRequestPath);
    return;
  }
  if (historyPage(result.requestConfig)) {
    if (!page.querySelector('[data-entry-page]')) { result.shouldSwap = false; result.isError = true; }
    else {
      const region = result.target.closest<HTMLElement>('.section-entries');
      if (region) region.dataset.expanded = 'true';
    }
    return;
  }
  if (background(result.requestConfig) && interacting()) {
    result.shouldSwap = false;
    return; // Do not remember an ETag for a result that has not been applied.
  }
  const etag = result.xhr.getResponseHeader('ETag');
  validator = etag ? {url: responseURL.pathname + responseURL.search, etag} : undefined;
  if (background(result.requestConfig)) {
    if (incoming.dataset.pageFingerprint === document.getElementById('page')?.dataset.pageFingerprint) {
      result.shouldSwap = false;
      return;
    }
    restore.set(result.xhr, preserveReadingState());
  }
  const theme = page.documentElement.getAttribute('data-theme');
  if (theme) document.documentElement.setAttribute('data-theme', theme);
  else document.documentElement.removeAttribute('data-theme');
});

document.addEventListener('htmx:afterSwap', event => {
  const result = (event as CustomEvent<HtmxResponseInfo>).detail;
  if (historyPage(result.requestConfig)) return;
  if (background(result.requestConfig)) restore.get(result.xhr)?.();
  else {
    document.getElementById('main')?.focus({preventScroll: true});
    window.scrollTo(0, 0);
    revealAnchor();
  }
});

// Uncached history must share navigation's timeout, validation and cancellation.
document.addEventListener('htmx:historyCacheMiss', event => {
  event.preventDefault();
  validator = undefined;
  const {path} = (event as CustomEvent<{path: string}>).detail;
  void htmx.ajax('get', path, {source: historySource!}).catch(() => {});
});

document.addEventListener('htmx:afterRequest', event => {
  const result = (event as CustomEvent<HtmxResponseInfo>).detail;
  if (result.successful) { failures = 0; status(); }
  schedule();
});

for (const name of ['htmx:responseError', 'htmx:sendError', 'htmx:timeout']) {
  document.addEventListener(name, () => {
    failures = Math.min(failures + 1, 4);
    status('Could not update results. Existing content is retained.');
    schedule();
  });
}

document.addEventListener('visibilitychange', () => schedule(document.hidden ? interval : 0));
window.addEventListener('online', () => schedule(0));
window.addEventListener('offline', () => clearTimeout(timer));
window.addEventListener('pagehide', () => clearTimeout(timer));
window.addEventListener('pageshow', () => schedule());
window.addEventListener('hashchange', revealAnchor);
revealAnchor();
schedule();
