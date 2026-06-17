# Nexus Memory — landing page

A small, dependency-free landing page for the **Nexus Memory Module**, built the
same way as [PersonaUI-Page](https://github.com/Sakushi-Dev/PersonaUI-Page):
plain HTML/CSS/JS, dark theme, and modular `sections/` fragments stitched together
at runtime by `js/loader.js`.

```
nexus-page/
  index.html            # landing — lists the sections to include
  docs.html             # documentation viewer (sidebar + markdown render)
  css/
    style.css           # the whole theme (dark · violet accent · Ubuntu + JetBrains Mono)
    docs.css            # docs viewer layout + rendered-markdown typography
  js/
    loader.js           # fetches sections/ + wires nav, copy buttons, reveal-on-scroll
    md.js               # tiny dependency-free Markdown → HTML renderer
    docs.js             # docs sidebar + hash routing + render
  sections/             # landing-page fragments (incl. io.html — the I/O diagram)
    background · nav · hero · features · architecture · showcase · io
    tech · install · cta · footer
  docs/                 # the module's markdown docs (rendered by docs.html)
    overview · ms7_multilayer · ms8_diary · architecture-io · final_validation
    contract-v3 · ms1…ms5
```

The docs under `docs/` are copies of the module's own `nexus-memory/docs/` (plus
its README as `overview.md`). To refresh them, re-copy the markdown — no build step.

## Run it

The sections are loaded with `fetch()`, which browsers block on `file://`, so
serve it over HTTP (any static server works):

```sh
cd nexus-page
python -m http.server 8000
# then open http://localhost:8000
```

## Deploy (GitHub Pages)

Push this folder to a repo and enable Pages (Settings → Pages → deploy from
branch). No build step — it's static. To host it at the repo root, move the
contents of `nexus-page/` up one level.

## Editing

- **Content** lives in `sections/*.html` — edit a fragment, refresh.
- **Add a section**: create `sections/foo.html`, then add
  `<div data-include="sections/foo.html"></div>` to `index.html` in the right spot.
- **Theme**: the colors, fonts and radii are CSS variables at the top of
  `css/style.css` (`--accent` is the violet; per-layer colors are `--l1`…`--l5`).
- Animations are opt-in per element via the `data-reveal` attribute.
