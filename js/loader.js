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
      const src = sel ? document.querySelector(sel) : btn.closest('.code')?.querySelector('code');
      if (!src) return;
      try {
        await navigator.clipboard.writeText(src.innerText.trim());
        const old = btn.textContent;
        btn.textContent = 'copied';
        btn.classList.add('done');
        setTimeout(() => { btn.textContent = old; btn.classList.remove('done'); }, 1400);
      } catch (_) { /* ignore */ }
    });
  });
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

document.addEventListener('DOMContentLoaded', async () => {
  await includeSections();
  setYear();
  wireSmoothScroll();
  wireMobileNav();
  wireNavScrollState();
  wireCopyButtons();
  wireReveal();
});
