/* Nexus Memory — section loader + tiny interactions.
   Mirrors the PersonaUI page approach: each [data-include] div is filled with a
   fragment from sections/ at runtime, then a few light behaviours are wired up.
   (Serve over HTTP — fetch() of local files is blocked on file://.) */

async function includeSections() {
  const nodes = Array.from(document.querySelectorAll('[data-include]'));
  await Promise.all(
    nodes.map(async (node) => {
      const url = node.getAttribute('data-include');
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(res.status);
        node.innerHTML = await res.text();
      } catch (err) {
        console.warn('Could not load', url, err);
      }
    })
  );
}

function setYear() {
  document.querySelectorAll('[data-year]').forEach((el) => {
    el.textContent = new Date().getFullYear();
  });
}

function wireSmoothScroll() {
  document.querySelectorAll('a[href^="#"]').forEach((a) => {
    a.addEventListener('click', (e) => {
      const id = a.getAttribute('href');
      if (id.length < 2) return;
      // brand / "#top" goes to the very top of the page, not the hero's edge
      if (id === '#top') {
        e.preventDefault();
        window.scrollTo({ top: 0, behavior: 'smooth' });
        closeMobileNav();
        return;
      }
      const target = document.querySelector(id);
      if (!target) return;
      e.preventDefault();
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      closeMobileNav();
    });
  });
}

function wireMobileNav() {
  const toggle = document.querySelector('[data-nav-toggle]');
  const links = document.querySelector('[data-nav-links]');
  if (!toggle || !links) return;
  toggle.addEventListener('click', () => {
    links.classList.toggle('open');
    toggle.classList.toggle('open');
  });
}

function closeMobileNav() {
  const links = document.querySelector('[data-nav-links]');
  const toggle = document.querySelector('[data-nav-toggle]');
  links && links.classList.remove('open');
  toggle && toggle.classList.remove('open');
}

function wireNavScrollState() {
  const nav = document.querySelector('[data-nav]');
  if (!nav) return;
  const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 24);
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });
}

function wireCopyButtons() {
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const sel = btn.getAttribute('data-copy');
      const codeEl = sel ? document.querySelector(sel) : btn.closest('.code')?.querySelector('code');
      if (!codeEl) return;
      let text;
      if (btn.hasAttribute('data-copy-cmd')) {
        // terminal blocks: copy only the command line(s) — drop comments + blanks
        const clone = codeEl.cloneNode(true);
        clone.querySelectorAll('.tok-c').forEach((c) => c.remove());
        text = clone.textContent.split('\n').map((l) => l.trim()).filter(Boolean).join('\n');
      } else {
        text = codeEl.innerText.trim();
      }
      try {
        await navigator.clipboard.writeText(text);
        const old = btn.textContent;
        btn.textContent = 'copied';
        btn.classList.add('done');
        setTimeout(() => { btn.textContent = old; btn.classList.remove('done'); }, 1400);
      } catch (_) { /* ignore */ }
    });
  });
}

/* Examples section: a script switcher (which example) plus, inside each code
   window, a Code/Output view toggle. "Run" flips the same window to a faithful
   *simulated* stdout that types out line by line — the page runs no Python, it
   just replays the pre-recorded output. The "code" tab flips back. */
function wireExamples() {
  // --- hero: top-level "which script" switcher (scoped to that group) ---
  const section = document.getElementById('examples');
  if (section) {
    const tabs = Array.from(section.querySelectorAll('[data-demo-tab]'));
    const panels = Array.from(section.querySelectorAll('[data-demo]'));
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        const key = tab.getAttribute('data-demo-tab');
        tabs.forEach((t) => t.classList.toggle('is-active', t === tab));
        panels.forEach((p) => { p.hidden = p.getAttribute('data-demo') !== key; });
      });
    });
  }

  // --- every runnable code window on the page: in-window code/output views + run ---
  document.querySelectorAll('[data-demo-run]').forEach((runBtn) => {
    const block = runBtn.closest('.code');
    if (!block) return;
    const viewTabs = Array.from(block.querySelectorAll('[data-view]'));
    const panes = Array.from(block.querySelectorAll('[data-pane]'));
    const outputTab = block.querySelector('[data-output-tab]');
    const out = block.querySelector('[data-term-out]');
    const src = block.querySelector('[data-term-src]');

    const showView = (key) => {
      viewTabs.forEach((t) => t.classList.toggle('is-active', t.getAttribute('data-view') === key));
      panes.forEach((p) => { p.hidden = p.getAttribute('data-pane') !== key; });
    };
    viewTabs.forEach((t) => {
      t.addEventListener('click', () => { if (!t.hidden) showView(t.getAttribute('data-view')); });
    });

    if (!runBtn || !out || !src) return;
    runBtn.addEventListener('click', () => {
      if (runBtn.dataset.running === '1') return;
      runBtn.dataset.running = '1';
      runBtn.textContent = 'running…';
      runBtn.classList.add('busy');
      if (outputTab) outputTab.hidden = false;  // reveal the "output" tab on first run
      showView('output');                       // flip the window to the output pane

      const lines = src.innerHTML.split('\n');
      out.innerHTML = '';
      let i = 0;
      const cursor = '<span class="term-cursor">▋</span>';
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      const step = () => {
        if (i >= lines.length) {
          out.innerHTML = lines.join('\n');
          runBtn.textContent = '↻ Re-run';
          runBtn.classList.remove('busy');
          runBtn.dataset.running = '0';
          return;
        }
        out.innerHTML = lines.slice(0, i + 1).join('\n') + cursor;
        out.scrollTop = out.scrollHeight;
        i += 1;
        setTimeout(step, reduce ? 0 : 90 + Math.random() * 70);
      };
      step();
    });
  });
}

/* Feature cards → detail overlay. Clicking a card opens a modal with a fuller
   write-up; while open, the page's background grid lights up (body.modal-open).
   Closes on backdrop click, the × button, or Escape. */
function wireCards() {
  const section = document.getElementById('features');
  if (!section) return;

  const DETAILS = {
    onepoint: {
      ico: '⌘', title: 'One entry point', tag: 'process()',
      body: 'Every interaction is a single call — <code>memory.process(payload)</code>. The payload is a dict or JSON string; an <code>action</code> field selects the handler. Requests are validated with pydantic (unknown keys are rejected), and <b>process() never raises</b> — failures come back as <code>{status:"error", error:"…"}</code>. So you can treat memory as a black box that always answers; just branch on <code>status</code>. Convenience wrappers (<code>inspect</code>, <code>forget</code>, <code>wait</code>, …) mirror the same handlers.',
    },
    local: {
      ico: '⬡', title: 'Local & offline', tag: 'HashingEmbedder · 768-dim',
      body: 'All state lives in one <code>.db</code> file backed by SQLite + sqlite-vec — no server, no daemon, no network. The default <code>HashingEmbedder</code> is a 768-dim blake2b feature hasher: deterministic, dependency-free, reproducible, and with <b>no model download</b>. Nothing leaves the machine on the default path — swap in a transformer or hosted embedder only if you want to.',
    },
    layers: {
      ico: '▤', title: '5-layer cognitive memory', tag: 'Atkinson–Shiffrin',
      body: 'A single <code>ingest</code> fans out across four layers — <b>Working</b> (a volatile RAM ring of recent turns), <b>Episodic</b> (raw dialogue + deterministic day-summaries), <b>Semantic</b> (decontextualized fact vectors), and <b>Procedural</b> (standing directives) — modeled on the Atkinson–Shiffrin memory framework. An optional fifth layer, the hierarchical <b>Diary</b>, is off by default. <code>assemble</code> returns one unified, layer-aware <code>&lt;memory_context&gt;</code>.',
    },
    agnostic: {
      ico: '⇄', title: 'Provider-agnostic', tag: 'pending_summaries · submit_summary',
      body: 'Nexus <b>never calls an LLM itself</b>. When a summary comes due it enqueues a job (prompt + context) into a handoff <em>outbox</em>. Your host drains it on <em>any</em> model — a cloud API, a local SLM, whatever — and submits the text back, which Nexus folds into the diary. Nexus owns the prompt, you own the model; summarization stays asynchronous and provider-agnostic by construction.',
    },
    transparent: {
      ico: '◎', title: 'Transparent & sovereign', tag: 'inspect · forget · pin',
      body: 'Your memories are yours: <code>inspect</code> store health and per-layer contents, <code>pin</code> and <code>update</code> facts, and <b>forget</b> by id or by free-text query. An opt-in regex PII filter masks emails / phones / names <em>before</em> embedding, and an optional SQLCipher hook encrypts the store — both stay off the critical path until you turn them on.',
    },
    retrieval: {
      ico: '⌖', title: 'Salient retrieval', tag: 'similarity × importance × decay',
      body: 'Retrieval embeds the query, over-retrieves via vec0 cosine KNN, then re-ranks by <code>similarity × importance × exp(-λ·days)</code> and filters by <code>min_score</code>. The result is a small, on-point context — capped at <code>top_k</code> — that mitigates the lost-in-the-middle problem instead of dumping everything into the prompt.',
    },
  };

  // build the overlay once
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML =
    '<div class="modal-dialog" role="dialog" aria-modal="true">' +
      '<button class="modal-close" aria-label="Close">✕</button>' +
      '<div class="modal-head"><div class="card-ico modal-ico"></div><h3 class="modal-title"></h3></div>' +
      '<p class="modal-body"></p>' +
      '<div class="card-tag modal-tag"></div>' +
    '</div>';
  document.body.appendChild(backdrop);
  const dlg = backdrop.querySelector('.modal-dialog');
  const elIco = backdrop.querySelector('.modal-ico');
  const elTitle = backdrop.querySelector('.modal-title');
  const elBody = backdrop.querySelector('.modal-body');
  const elTag = backdrop.querySelector('.modal-tag');

  const open = (key) => {
    const d = DETAILS[key];
    if (!d) return;
    elIco.textContent = d.ico;
    elTitle.textContent = d.title;
    elBody.innerHTML = d.body;
    elTag.textContent = d.tag;
    backdrop.classList.add('open');
    document.body.classList.add('modal-open');   // → full background grid glows
  };
  const close = () => {
    backdrop.classList.remove('open');
    document.body.classList.remove('modal-open');
  };

  section.addEventListener('click', (e) => {
    const card = e.target.closest('.card');
    if (card && section.contains(card)) open(card.getAttribute('data-card'));
  });
  section.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const card = e.target.closest('.card');
    if (card) { e.preventDefault(); open(card.getAttribute('data-card')); }
  });
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
  backdrop.querySelector('.modal-close').addEventListener('click', close);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

/* Marquee: clone each [data-marquee] track's items once so a CSS translateX of
   -50% loops seamlessly. Clones are aria-hidden (pure decoration). */
function wireMarquee() {
  document.querySelectorAll('[data-marquee]').forEach((track) => {
    Array.from(track.children).forEach((el) => {
      const clone = el.cloneNode(true);
      clone.setAttribute('aria-hidden', 'true');
      track.appendChild(clone);
    });
  });
}

/* Generic tab groups: any .tabset wires its own [data-tab] buttons to the
   matching [data-tabpane] panels, independently of other tabsets on the page
   (e.g. the Quickstart install options and the usage examples). */
function wireTabs() {
  document.querySelectorAll('.tabset').forEach((set) => {
    const tabs = Array.from(set.querySelectorAll('[data-tab]'));
    const panes = Array.from(set.querySelectorAll('[data-tabpane]'));
    if (!tabs.length) return;

    // optional: a description element this tabset types into as tabs change.
    // each tab carries its blurb in [data-desc]; the target is [data-desc-target].
    const descEl = set.getAttribute('data-desc-target')
      ? document.querySelector(set.getAttribute('data-desc-target'))
      : null;
    let typeToken = 0;
    const typeDesc = (text) => {
      if (!descEl || text == null) return;
      const token = ++typeToken;             // newer calls supersede older typers
      const cursor = '<span class="type-cursor">▋</span>';
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        descEl.textContent = text;
        return;
      }
      let i = 0;
      const step = () => {
        if (typeToken !== token) return;     // a later tab click took over
        descEl.innerHTML = text.slice(0, i) + (i < text.length ? cursor : '');
        if (i >= text.length) return;
        i += 1;
        setTimeout(step, 12 + Math.random() * 24);
      };
      step();
    };

    const activate = (tab, type) => {
      const key = tab.getAttribute('data-tab');
      tabs.forEach((t) => t.classList.toggle('is-active', t === tab));
      panes.forEach((p) => { p.hidden = p.getAttribute('data-tabpane') !== key; });
      if (type) typeDesc(tab.getAttribute('data-desc'));
    };

    tabs.forEach((tab) => tab.addEventListener('click', () => activate(tab, true)));

    // type out the initially-active tab's blurb once on load
    if (descEl) typeDesc((tabs.find((t) => t.classList.contains('is-active')) || tabs[0]).getAttribute('data-desc'));
  });
}

/* Every section description (.lead) types itself out the first time it scrolls
   into view. HTML-aware: inline tags (<code>, <b>) and entities are emitted
   whole so formatting survives. The tab-driven Quickstart blurb (#talk-desc) is
   left to wireTabs(). Honours prefers-reduced-motion. */
function wireTypewriters() {
  const leads = Array.from(document.querySelectorAll('.lead')).filter((el) => el.id !== 'talk-desc');
  if (!leads.length) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return; // leave static

  const tokenRe = /(<[^>]+>)|(&[#a-zA-Z0-9]+;)|([\s\S])/g;
  const prep = (el) => {
    const tokens = [];
    let m;
    while ((m = tokenRe.exec(el.innerHTML))) tokens.push(m[0]);
    el._tokens = tokens;
    el.style.minHeight = el.offsetHeight + 'px';  // reserve space so nothing jumps
    el.innerHTML = '';
  };
  const type = (el) => {
    if (el._typing) return;
    el._typing = true;
    const tokens = el._tokens;
    const cursor = '<span class="type-cursor">▋</span>';
    let i = 0;
    const step = () => {
      if (i >= tokens.length) { el.innerHTML = tokens.join(''); return; }
      const isTag = tokens[i].startsWith('<') || tokens[i].startsWith('&');
      el.innerHTML = tokens.slice(0, i).join('') + cursor;
      i += 1;
      setTimeout(step, isTag ? 0 : 10 + Math.random() * 22);
    };
    step();
  };

  if (!('IntersectionObserver' in window)) { leads.forEach((el) => { prep(el); type(el); }); return; }
  leads.forEach(prep);
  const io = new IntersectionObserver(
    (entries) => entries.forEach((e) => { if (e.isIntersecting) { type(e.target); io.unobserve(e.target); } }),
    { threshold: 0.3 }
  );
  leads.forEach((el) => io.observe(el));
}

function wireReveal() {
  const els = document.querySelectorAll('[data-reveal]');
  if (!('IntersectionObserver' in window)) {
    els.forEach((el) => el.classList.add('in'));
    return;
  }
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          io.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: '0px 0px -40px 0px' }
  );
  els.forEach((el) => io.observe(el));
}

/* The bright grid "spotlight" is driven entirely from JS once loaded.
   - cursor inside the viewport → it tracks the pointer and the wander is paused
     (it stays parked under the cursor even while the mouse is held still);
   - cursor leaves the viewport (or no fine pointer) → it resumes drifting
     sporadically, gliding on smoothly from wherever it currently sits.
   Both share --sx/--sy so handing over never jumps. */
function wireGridSpot() {
  const spot = document.querySelector('.bg-grid-spot');
  if (!spot) return;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  spot.classList.add('js-driven');
  if (reduce) return; // honour reduced-motion: leave the spot static

  const points = [[22, 28], [74, 19], [87, 63], [39, 81], [11, 54], [59, 43]];
  let pi = 0, wanderTimer = 0, tracking = false;

  const setPos = (x, y) => {
    spot.style.setProperty('--sx', x.toFixed(2) + '%');
    spot.style.setProperty('--sy', y.toFixed(2) + '%');
  };
  function wanderStep() {
    if (tracking) return;
    pi = (pi + 1 + Math.floor(Math.random() * (points.length - 1))) % points.length;
    spot.classList.add('drifting');
    setPos(points[pi][0] + (Math.random() * 8 - 4), points[pi][1] + (Math.random() * 8 - 4));
    wanderTimer = setTimeout(wanderStep, 3200 + Math.random() * 3200);
  }
  function startWander() {
    tracking = false;
    clearTimeout(wanderTimer);
    wanderTimer = setTimeout(wanderStep, 700); // glide off shortly after the cursor leaves
  }

  startWander(); // wander until (and whenever) the cursor is away

  if (window.matchMedia('(pointer: fine)').matches) {
    let raf = 0, px = 50, py = 28;
    const apply = () => { raf = 0; setPos(px, py); };
    window.addEventListener('pointermove', (e) => {
      tracking = true;
      clearTimeout(wanderTimer);
      spot.classList.remove('drifting'); // snappy follow, not the slow glide
      px = (e.clientX / window.innerWidth) * 100;
      py = (e.clientY / window.innerHeight) * 100;
      if (!raf) raf = requestAnimationFrame(apply);
    }, { passive: true });
    // pointer leaves the viewport entirely → hand back to the wander
    document.addEventListener('mouseout', (e) => { if (!e.relatedTarget) startWander(); });
    window.addEventListener('blur', startWander);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  await includeSections();
  setYear();
  wireSmoothScroll();
  wireMobileNav();
  wireNavScrollState();
  wireCopyButtons();
  wireExamples();
  wireTabs();
  wireCards();
  wireMarquee();
  wireTypewriters();
  wireReveal();
  wireGridSpot();
});
