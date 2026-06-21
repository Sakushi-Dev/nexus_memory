/* Docs viewer — path-based hash routing, grouped sidebar, full-text search and
   on-the-fly markdown render (no dependencies).
   Serve over HTTP (fetch() of the .md files is blocked on file://):
     python -m http.server   →   http://localhost:8000/docs.html         */

const REPO = 'https://github.com/Sakushi-Dev/nexus_memory';
const REPO_BLOB = REPO + '/blob/main/';

/* Every page in docs/. `id` is the path under docs/ without the .md extension
   and doubles as the hash route (e.g. #usage/api-reference). */
const DOCS = [
  { id: 'index',                            title: 'Overview',            file: 'index.md' },

  { id: 'architecture/overview',            title: 'Overview',            file: 'architecture/overview.md' },
  { id: 'architecture/memory-layers',       title: 'Memory layers',       file: 'architecture/memory-layers.md' },
  { id: 'architecture/diary-layer',         title: 'Diary layer',         file: 'architecture/diary-layer.md' },
  { id: 'architecture/retrieval-and-scoring', title: 'Retrieval & scoring', file: 'architecture/retrieval-and-scoring.md' },
  { id: 'architecture/persistence',         title: 'Persistence',         file: 'architecture/persistence.md' },
  { id: 'architecture/extension-points',    title: 'Extension points',    file: 'architecture/extension-points.md' },

  { id: 'io/request-response',              title: 'Request & response',  file: 'io/request-response.md' },
  { id: 'io/data-flow',                     title: 'Data flow',           file: 'io/data-flow.md' },

  { id: 'usage/getting-started',            title: 'Getting started',     file: 'usage/getting-started.md' },
  { id: 'usage/api-reference',              title: 'API reference',       file: 'usage/api-reference.md' },
  { id: 'usage/configuration',              title: 'Configuration',       file: 'usage/configuration.md' },
  { id: 'usage/embedders',                  title: 'Embedders',           file: 'usage/embedders.md' },
  { id: 'usage/transparency',               title: 'Transparency',        file: 'usage/transparency.md' },

  { id: 'use-cases/agent-memory',           title: 'Agent memory',        file: 'use-cases/agent-memory.md' },
  { id: 'use-cases/multiple-agents',        title: 'Multiple agents',     file: 'use-cases/multiple-agents.md' },
  { id: 'use-cases/behavioral-rules',       title: 'Behavioral rules',    file: 'use-cases/behavioral-rules.md' },
  { id: 'use-cases/hierarchical-diary',     title: 'Hierarchical diary',  file: 'use-cases/hierarchical-diary.md' },
  { id: 'use-cases/privacy-and-encryption', title: 'Privacy & encryption', file: 'use-cases/privacy-and-encryption.md' },

  { id: 'configuration/nexus-config',       title: 'NexusConfig',         file: 'configuration/nexus-config.md' },
  { id: 'configuration/diary-config',       title: 'DiaryConfig',         file: 'configuration/diary-config.md' },
  { id: 'configuration/tuning',             title: 'Tuning',              file: 'configuration/tuning.md' },

  { id: 'changelog/index',                  title: 'Changelog',           file: 'changelog/index.md' },
  { id: 'changelog/0.7.0',                  title: 'v0.7.0',              file: 'changelog/0.7.0.md' },
  { id: 'changelog/0.6.0',                  title: 'v0.6.0',              file: 'changelog/0.6.0.md' },
  { id: 'changelog/0.5.1',                  title: 'v0.5.1',              file: 'changelog/0.5.1.md' },
  { id: 'changelog/0.5.0',                  title: 'v0.5.0',              file: 'changelog/0.5.0.md' },
  { id: 'changelog/0.4.2',                  title: 'v0.4.2',              file: 'changelog/0.4.2.md' },
  { id: 'changelog/0.4.1',                  title: 'v0.4.1',              file: 'changelog/0.4.1.md' },
  { id: 'changelog/0.4.0',                  title: 'v0.4.0',              file: 'changelog/0.4.0.md' },
  { id: 'changelog/0.3.5',                  title: 'v0.3.5',              file: 'changelog/0.3.5.md' },
  { id: 'changelog/0.3.4',                  title: 'v0.3.4',              file: 'changelog/0.3.4.md' },
  { id: 'changelog/0.3.3',                  title: 'v0.3.3',              file: 'changelog/0.3.3.md' },
  { id: 'changelog/0.3.2',                  title: 'v0.3.2',              file: 'changelog/0.3.2.md' },
  { id: 'changelog/0.3.1',                  title: 'v0.3.1',              file: 'changelog/0.3.1.md' },
];

/* Sidebar grouping (mirrors the documentation map in index.md). */
const GROUPS = [
  { label: 'Start',         ids: ['index'] },
  { label: 'Architecture',  ids: ['architecture/overview', 'architecture/memory-layers', 'architecture/diary-layer', 'architecture/retrieval-and-scoring', 'architecture/persistence', 'architecture/extension-points'] },
  { label: 'I / O',         ids: ['io/request-response', 'io/data-flow'] },
  { label: 'Usage',         ids: ['usage/getting-started', 'usage/api-reference', 'usage/configuration', 'usage/embedders', 'usage/transparency'] },
  { label: 'Use cases',     ids: ['use-cases/agent-memory', 'use-cases/multiple-agents', 'use-cases/behavioral-rules', 'use-cases/hierarchical-diary', 'use-cases/privacy-and-encryption'] },
  { label: 'Configuration', ids: ['configuration/nexus-config', 'configuration/diary-config', 'configuration/tuning'] },
  { label: 'Changelog',     ids: ['changelog/index', 'changelog/0.7.0', 'changelog/0.6.0', 'changelog/0.5.1', 'changelog/0.5.0', 'changelog/0.4.2', 'changelog/0.4.1', 'changelog/0.4.0', 'changelog/0.3.5', 'changelog/0.3.4', 'changelog/0.3.3', 'changelog/0.3.2', 'changelog/0.3.1'] },
];

const byId = Object.fromEntries(DOCS.map((d) => [d.id, d]));
const DEFAULT_ID = 'index';

/* ---------------------------------------------------------------- helpers --- */

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* Mirror of md.js slug() so search anchors match the rendered heading ids. */
function slug(s) {
  return String(s)
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s/g, '-')
    .replace(/^-+|-+$/g, '');
}

/* Strip the markdown that shows up in headings/snippets so search reads cleanly.
   Only asterisk emphasis is removed — single underscores are kept so identifiers
   like `top_k`, `min_score` and `decay_lambda` stay searchable. */
function stripMd(s) {
  return String(s)
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*?/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/* Resolve a relative href found inside `fromRepoPath` to a repo-root path. */
function resolveRepoPath(fromRepoPath, href) {
  const stack = fromRepoPath.split('/').slice(0, -1); // directory of the doc
  for (const part of href.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') { if (stack.length) stack.pop(); continue; }
    stack.push(part);
  }
  return stack.join('/');
}

/* --------------------------------------------------------------- routing --- */

function parseHash() {
  const raw = decodeURIComponent(location.hash.replace(/^#/, ''));
  const [path, anchor] = raw.split('::');
  return { id: byId[path] ? path : DEFAULT_ID, anchor: anchor || '' };
}

let renderedId = null;

function setActive(id) {
  document.querySelectorAll('.docs-link').forEach((a) => {
    a.classList.toggle('active', a.getAttribute('data-id') === id);
  });
}

/* Rewrite the links inside a freshly rendered doc:
   - #anchor            → #<thisDoc>::anchor   (stay on page, scroll)
   - relative .md       → #<resolved doc id>   (SPA route)
   - anything escaping docs/ (src, examples, LICENSE …) → GitHub blob URL  */
function rewriteLinks(container, doc) {
  const fromRepoPath = 'docs/' + doc.file;
  container.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href');
    if (!href || /^(https?:|mailto:)/.test(href)) return;

    if (href.startsWith('#')) {
      a.setAttribute('href', '#' + doc.id + '::' + href.slice(1));
      return;
    }

    const [pathPart, frag] = href.split('#');
    const repoPath = resolveRepoPath(fromRepoPath, pathPart);

    if (repoPath.startsWith('docs/') && repoPath.endsWith('.md')) {
      const targetId = repoPath.slice('docs/'.length, -'.md'.length);
      if (byId[targetId]) {
        a.setAttribute('href', '#' + targetId + (frag ? '::' + frag : ''));
        return;
      }
    }
    // Falls outside the docs tree (or unknown) → point at the repo on GitHub.
    a.setAttribute('href', REPO_BLOB + repoPath + (frag ? '#' + frag : ''));
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener');
  });
}

async function render({ id, anchor }) {
  const doc = byId[id] || byId[DEFAULT_ID];
  const content = document.getElementById('docs-content');
  setActive(doc.id);
  document.title = `${doc.title} · Nexus Memory docs`;

  if (renderedId !== doc.id) {
    try {
      const res = await fetch('docs/' + doc.file, { cache: 'no-cache' });
      if (!res.ok) throw new Error(res.status);
      content.innerHTML = window.mdToHtml(await res.text());
      rewriteLinks(content, doc);
    } catch (err) {
      content.innerHTML = `<h1>Couldn't load this doc</h1>
        <p>Failed to fetch <code>docs/${esc(doc.file)}</code> (${esc(String(err))}). Are you serving over HTTP?
        Try <code>python -m http.server</code> from the project folder.</p>`;
    }
    renderedId = doc.id;
  }

  closeSidebar();
  if (anchor) {
    const target = document.getElementById(anchor);
    if (target) { target.scrollIntoView({ block: 'start' }); return; }
  }
  content.parentElement.scrollTop = 0;
  window.scrollTo(0, 0);
}

/* --------------------------------------------------------------- sidebar --- */

function buildSidebar() {
  const nav = document.getElementById('docs-nav');
  nav.innerHTML = GROUPS.map((g) => `
    <div class="docs-group">
      <p class="docs-group-label">${esc(g.label)}</p>
      ${g.ids.map((id) => {
        const d = byId[id];
        return d ? `<a class="docs-link" data-id="${d.id}" href="#${d.id}">${esc(d.title)}</a>` : '';
      }).join('')}
    </div>`).join('');
}

function wireSidebarToggle() {
  const btn = document.querySelector('[data-side-toggle]');
  const shell = document.querySelector('.docs-shell');
  if (!btn || !shell) return;
  btn.addEventListener('click', () => shell.classList.toggle('side-open'));
}
function closeSidebar() {
  document.querySelector('.docs-shell')?.classList.remove('side-open');
}

/* ---------------------------------------------------------------- search --- */

let SECTIONS = [];
let indexState = 'idle'; // idle | building | ready

function sectionsOf(doc, src) {
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  const sections = [];
  let cur = { docId: doc.id, docTitle: doc.title, heading: doc.title, anchor: '', text: '' };
  let inFence = false;
  for (const line of lines) {
    if (/^```/.test(line)) { inFence = !inFence; continue; }
    if (!inFence) {
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        if (cur.text.trim() || cur.heading) sections.push(cur);
        const txt = h[2].replace(/\s+#*\s*$/, '');
        cur = { docId: doc.id, docTitle: doc.title, heading: stripMd(txt), anchor: slug(txt), text: '' };
        continue;
      }
    }
    cur.text += ' ' + line;
  }
  sections.push(cur);
  return sections.map((s) => ({ ...s, text: stripMd(s.text) })).filter((s) => s.text || s.heading);
}

async function buildIndex() {
  if (indexState !== 'idle') return;
  indexState = 'building';
  const parts = await Promise.all(DOCS.map(async (d) => {
    try {
      const res = await fetch('docs/' + d.file, { cache: 'no-cache' });
      if (!res.ok) return [];
      return sectionsOf(d, await res.text());
    } catch (_) { return []; }
  }));
  SECTIONS = parts.flat();
  indexState = 'ready';
}

function occ(haystack, term) {
  let n = 0, i = 0;
  while ((i = haystack.indexOf(term, i)) !== -1) { n++; i += term.length; }
  return n;
}

function search(q) {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const scored = [];
  for (const s of SECTIONS) {
    const title = s.docTitle.toLowerCase();
    const head = s.heading.toLowerCase();
    const body = s.text.toLowerCase();
    if (!terms.every((t) => title.includes(t) || head.includes(t) || body.includes(t))) continue;
    let score = 0;
    for (const t of terms) {
      score += occ(title, t) * 6 + occ(head, t) * 4 + occ(body, t);
    }
    scored.push({ s, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, 25);
}

function snippet(text, term) {
  const lower = text.toLowerCase();
  let i = term ? lower.indexOf(term) : 0;
  if (i < 0) i = 0;
  const start = Math.max(0, i - 50);
  let slice = text.slice(start, start + 150).trim();
  if (start > 0) slice = '… ' + slice;
  if (start + 150 < text.length) slice += ' …';
  return slice;
}

function highlight(text, terms) {
  let html = esc(text);
  for (const t of terms) {
    if (!t) continue;
    const re = new RegExp('(' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
    html = html.replace(re, '<mark>$1</mark>');
  }
  return html;
}

function renderResults(q) {
  const box = document.getElementById('docs-search-results');
  const nav = document.getElementById('docs-nav');
  if (!q.trim()) {
    box.hidden = true; box.innerHTML = '';
    nav.hidden = false;
    return;
  }
  nav.hidden = true;
  box.hidden = false;

  if (indexState !== 'ready') {
    box.innerHTML = '<p class="docs-results-empty">Indexing docs…</p>';
    return;
  }
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hits = search(q);
  if (!hits.length) {
    box.innerHTML = `<p class="docs-results-empty">No matches for “${esc(q)}”.</p>`;
    return;
  }
  box.innerHTML = hits.map(({ s }, idx) => {
    const href = '#' + s.docId + (s.anchor ? '::' + s.anchor : '');
    const crumb = s.heading && s.heading !== s.docTitle
      ? `${esc(s.docTitle)} <span class="sep">›</span> ${highlight(s.heading, terms)}`
      : esc(s.docTitle);
    return `<a class="docs-result${idx === 0 ? ' sel' : ''}" href="${href}">
      <span class="docs-result-crumb">${crumb}</span>
      <span class="docs-result-snip">${highlight(snippet(s.text || s.heading, terms[0]), terms)}</span>
    </a>`;
  }).join('');
}

function moveSelection(dir) {
  const items = [...document.querySelectorAll('.docs-result')];
  if (!items.length) return;
  let i = items.findIndex((el) => el.classList.contains('sel'));
  items[i]?.classList.remove('sel');
  i = (i + dir + items.length) % items.length;
  items[i].classList.add('sel');
  items[i].scrollIntoView({ block: 'nearest' });
}

function wireSearch() {
  const input = document.getElementById('docs-search-input');
  const box = document.getElementById('docs-search-results');
  if (!input || !box) return;

  const onChange = () => renderResults(input.value);
  input.addEventListener('focus', () => { buildIndex().then(() => { if (input.value) renderResults(input.value); }); });
  input.addEventListener('input', () => { onChange(); buildIndex().then(onChange); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveSelection(-1); }
    else if (e.key === 'Enter') {
      const sel = box.querySelector('.docs-result.sel') || box.querySelector('.docs-result');
      if (sel) { e.preventDefault(); location.hash = sel.getAttribute('href'); clearSearch(); }
    } else if (e.key === 'Escape') { clearSearch(); input.blur(); }
  });
  box.addEventListener('click', (e) => { if (e.target.closest('.docs-result')) clearSearch(); });

  // Global shortcuts: "/" or Cmd/Ctrl+K focuses search.
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || '');
    if ((e.key === '/' && !typing) || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k')) {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
}

function clearSearch() {
  const input = document.getElementById('docs-search-input');
  if (input) input.value = '';
  renderResults('');
}

/* ------------------------------------------------------------------ boot --- */

document.addEventListener('DOMContentLoaded', () => {
  buildSidebar();
  wireSidebarToggle();
  wireSearch();
  render(parseHash());
  window.addEventListener('hashchange', () => render(parseHash()));
});
