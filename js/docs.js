/* Docs viewer — sidebar + hash routing + markdown render (no dependencies).
   Serve over HTTP (fetch() of the .md files is blocked on file://). */

const DOCS = [
  { group: 'Start', items: [
    { id: 'overview', title: 'Overview', file: 'overview.md' },
  ]},
  { group: 'Architecture', items: [
    { id: 'multilayer', title: 'Multi-layer memory', file: 'ms7_multilayer.md' },
    { id: 'diary',      title: 'Layer V · the diary', file: 'ms8_diary.md' },
    { id: 'io',         title: 'Input / Output map',  file: 'architecture-io.md' },
  ]},
  { group: 'Reference', items: [
    { id: 'validation',  title: 'Validation report',   file: 'final_validation.md' },
    { id: 'contract-v3', title: 'Diary contract (v3)',  file: 'contract-v3.md' },
  ]},
  { group: 'Build log', items: [
    { id: 'ms1', title: 'MS1 · Foundation',         file: 'ms1_results.md' },
    { id: 'ms2', title: 'MS2 · Orchestrator',       file: 'ms2_results.md' },
    { id: 'ms3', title: 'MS3 · Reader & scoring',   file: 'ms3_results.md' },
    { id: 'ms4', title: 'MS4 · Writer loop',        file: 'ms4_results.md' },
    { id: 'ms5', title: 'MS5 · Privacy & security', file: 'ms5_results.md' },
  ]},
];

const flat = DOCS.flatMap((g) => g.items);
const byId = Object.fromEntries(flat.map((d) => [d.id, d]));

function buildSidebar() {
  const nav = document.getElementById('docs-nav');
  nav.innerHTML = DOCS.map((g) => `
    <div class="docs-group">
      <p class="docs-group-label">${g.group}</p>
      ${g.items.map((d) => `<a class="docs-link" data-id="${d.id}" href="#${d.id}">${d.title}</a>`).join('')}
    </div>`).join('');
}

function setActive(id) {
  document.querySelectorAll('.docs-link').forEach((a) => {
    a.classList.toggle('active', a.getAttribute('data-id') === id);
  });
}

async function render(id) {
  const doc = byId[id] || flat[0];
  const content = document.getElementById('docs-content');
  setActive(doc.id);
  document.title = `${doc.title} · Nexus Memory docs`;
  try {
    const res = await fetch('docs/' + doc.file);
    if (!res.ok) throw new Error(res.status);
    content.innerHTML = window.mdToHtml(await res.text());
  } catch (err) {
    content.innerHTML = `<h1>Couldn't load this doc</h1>
      <p>Failed to fetch <code>docs/${doc.file}</code> (${err}). Are you serving over HTTP?
      Try <code>python -m http.server</code> from the project folder.</p>`;
  }
  content.parentElement.scrollTop = 0;
  window.scrollTo(0, 0);
  closeSidebar();
}

function currentId() {
  const id = location.hash.replace(/^#/, '');
  return byId[id] ? id : flat[0].id;
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

document.addEventListener('DOMContentLoaded', () => {
  buildSidebar();
  wireSidebarToggle();
  render(currentId());
  window.addEventListener('hashchange', () => render(currentId()));
});
