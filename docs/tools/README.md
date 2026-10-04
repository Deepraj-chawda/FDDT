# PDF build (`docs-pdf/`)

`build-pdfs.mjs` regenerates every PDF in `docs-pdf/` from the Markdown in `docs/`, the root `README.md`
and `docs/api/openapi.json`. The PDFs are build output — edit the Markdown, then rebuild.

```bash
cd docs/tools
npm install          # once: marked, mermaid, puppeteer-core
npm run build        # all PDFs, ~1 minute
node build-pdfs.mjs --only 26 --only 24   # just the files whose names start with these prefixes
```

## How it works

1. Markdown is rendered to HTML with `marked` (GitHub-flavoured). `mermaid` code blocks are drawn in the
   page with Mermaid and scaled to fit one A4 page; images (the user-manual screenshots) are embedded.
2. `24-API-Endpoint-Reference-OpenAPI.pdf` is generated directly from `openapi.json`: endpoint index,
   one section per operation (auth, parameters, request body, responses) and every data model.
   Regenerate the snapshot first (command in [../api/README.md](../api/README.md#regenerating-the-snapshot)).
3. Each page is printed to A4 PDF by a **locally installed Chrome or Edge** via `puppeteer-core` — nothing is
   downloaded. Set `CHROME_PATH` to use a specific Chromium-based browser.
4. `FDDT-Complete-Documentation.pdf` concatenates every document (including the user manual and the
   OpenAPI reference) behind a cover and a linked contents page.

Every PDF gets a bookmark outline and a footer (`FDDT — Fraud Document Detection Tool · <title>`, page
x of y). The user manual also gets a cover and a contents page (configured in `DOC_LIST`).

## Links

- Links within a document stay clickable. In the combined PDF, links between documents jump to the
  right place too.
- In the individual PDFs, references to *other* documents are kept as plain text: Chrome stores link
  targets as absolute `file://` paths, which would break as soon as the PDFs are copied elsewhere.
- The build prints a warning for any `#fragment` link whose heading no longer exists — fix the Markdown
  link when you see one.

## Database docs

`gen_db_docs.py` (Python, run with the backend's virtualenv) regenerates `docs/database/schema.md` and
`erd.md` from the **live, migrated** Postgres — tables, columns, indexes, constraints, enums, Row-Level
Security policies and the app roles' grants:

```bash
cd backend
.venv/Scripts/python ../docs/tools/gen_db_docs.py      # Linux/macOS: .venv/bin/python
```

It connects with `DOCS_DATABASE_URL`, else `DATABASE_URL`, else the local docker owner URL. Per-table
purpose sentences live in the script (`PURPOSE`). Re-run after every migration, then rebuild the PDFs.

## Adding a document

Add an entry to `DOC_LIST` in `build-pdfs.mjs` (`file` = output name without `.pdf`, `src` = path from the
repo root, `group` = its section in the combined PDF's contents), then rebuild.
