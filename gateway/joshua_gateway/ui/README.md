# Joshua UI

The shared look of Joshua's own interfaces: the viewer and the addons that
ship with Joshua. This folder holds the master copy of the stylesheet.

| File | What it is |
| --- | --- |
| `joshua-ui.css` | Design tokens (`--j-*`) and components (`.j-*`). Dark only. |

The images of the look are the ones the viewer serves,
`gateway/frontend/public/`: `logo-512.png` and `logo-64.png` (the logo, for
app bars and sidebars), `apple-touch-icon.png` (180px, for a phone home
screen) and `favicon-32.png` (the browser tab icon).

## Using it in an addon

Using this stylesheet is optional. An addon written by someone else can look
however its author wants.

An app that does use it keeps its own copy of these files and does not edit
that copy: the React app of the viewer imports this master from
`gateway/frontend/src/index.css` and copies the images into its own
`public/`. To change the look, change the master here, bump the version in
the header comment of `joshua-ui.css`, and copy the files into each app again.
Each copy has the version in its header, so you can see which copies are out
of date.

```html
<link rel="stylesheet" href="./joshua-ui.css">
<link rel="icon" href="./favicon-32.png">
<link rel="apple-touch-icon" href="./apple-touch-icon.png">
```

## Page skeleton

```html
<body>
  <div class="j-shell has-sidebar">
    <aside class="j-sidebar">…desktop nav (.j-brand, .j-nav-item)…</aside>
    <div class="j-main">
      <header class="j-appbar">
        <img class="j-logo" src="./logo-64.png" alt="">
        <a class="j-appbar-title" href="/"><small>Joshua</small><strong>Pantry</strong></a>
        <span class="j-spacer"></span>
        <button class="j-icon-btn" aria-label="Search">…</button>
      </header>
      <nav class="j-tabs mobile-only">
        <a class="j-tab" aria-current="page" href="#">Inventory</a>
        <a class="j-tab" href="#">Analytics</a>
      </nav>
      <main class="j-content">…</main>
    </div>
  </div>
</body>
```

On screens under 760px wide the sidebar hides and the app bar and tabs carry
navigation. `.j-tabs.mobile-only` hides once the sidebar shows.

## Components

- Buttons: `.j-btn` with `.primary`, `.ghost`, `.danger`, `.sm`, `.block`;
  `.j-icon-btn` for icon-only buttons.
- Inputs: `.j-field` wraps an `input`, `select` or `textarea`, with optional
  icons; `.j-field-label` above it, `.j-error` below it.
- Filters: `.j-chips` > `.j-chip` with `aria-pressed="true"` when on.
- Status: `.j-badge` with `.ok`, `.warn`, `.bad`, `.accent`, `.plain`;
  `.j-dot` for a bare status dot.
- Lists: `.j-list` > `.j-list-row` > `.j-list-main` > `.j-list-title` and
  `.j-list-sub`. Add `.done` to a row to strike it through.
- Controls: `input.j-check`, `input.j-toggle`, `.j-stepper`.
- Cards and figures: `.j-card`, `.j-stats` > `.j-stat`.
- Feedback: `.j-toast` (`.float` to pin it to the bottom), `.j-empty`,
  `.j-spinner`.
- Documents: `.j-crumbs`, `.j-doc-title`, `.j-meta`, `.j-prose` for rendered
  markdown, `.j-entry` for a journal entry, `.j-table` in `.j-table-wrap`.
- Phone helpers: `.j-bottom-bar` for a quick-add field pinned to the bottom.

Fonts: the stack prefers Figtree and falls back to the system font. Nothing
is fetched from a font host.
