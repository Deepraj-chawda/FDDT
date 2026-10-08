// Builds every PDF in docs-pdf/ from the Markdown in docs/ (plus the root
// README.md and docs/api/openapi.json).
//
//   cd docs/tools && npm install && npm run build
//
// Markdown -> HTML with `marked`, Mermaid diagrams rendered in the page, then
// printed to A4 PDF by a locally installed Chrome or Edge through
// `puppeteer-core` (no browser download). Set CHROME_PATH to use a specific
// browser binary. Output file names are fixed (DOCS below) so links to
// docs-pdf/ from elsewhere keep working.
//
// Usage: node build-pdfs.mjs [--only <file-prefix>]...   e.g. --only 26 --only 24
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

import { Marked } from "marked"
import puppeteer from "puppeteer-core"

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, "..", "..")
const DOCS = path.join(ROOT, "docs")
const OUT_DIR = path.join(ROOT, "docs-pdf")
const TMP_DIR = path.join(os.tmpdir(), "fddt-docs-pdf")
const MERMAID_JS = path.join(HERE, "node_modules", "mermaid", "dist", "mermaid.min.js")
const PRODUCT = "FDDT — Fraud Document Detection Tool"

// Every generated PDF: source, output name, and the group it belongs to in the
// combined book (in book order).
const DOC_LIST = [
  { file: "00-Project-Overview-README", src: "README.md", group: "Project" },
  { file: "01-Documentation-Index", src: "docs/README.md", group: "Introduction" },
  {
    file: "26-User-Manual",
    src: "docs/user-manual.md",
    group: "User manual",
    cover: {
      title: "Fraud Document Detection Tool",
      tag: "(FDDT)",
      subtitle: "User Manual",
      meta: [
        "A guide for submitters, Reviewer L1 / Reviewer L2 and platform administrators",
        "Describes the application as implemented — companion to the technical documentation in docs/",
      ],
      note:
        "This manual is direct about what the automated checks can and cannot prove. No signature is ever " +
        "“verified,” AI-generated-content detection is experimental, and every scored case still goes to a " +
        "human reviewer — nothing here auto-approves anything.",
    },
  },
  {
    file: "32-Company-User-Manual",
    src: "docs/COMPANY_USER_MANUAL.md",
    group: "User manual",
    cover: {
      title: "Fraud Document Detection Tool",
      tag: "(FDDT)",
      subtitle: "Company User Manual",
      meta: [
        "A guide for client organizations: User (Submitter), Reviewer L1 and Reviewer L2",
        "Describes the application as implemented — companion to the technical documentation in docs/",
      ],
    },
  },
  {
    file: "33-Platform-Admin-Manual",
    src: "docs/PLATFORM_ADMIN_MANUAL.md",
    group: "User manual",
    cover: {
      title: "Fraud Document Detection Tool",
      tag: "(FDDT)",
      subtitle: "Platform Administrator Manual",
      meta: [
        "Tenant provisioning, user administration, rule templates, queues, usage and audit history",
        "Describes the application as implemented — companion to the technical documentation in docs/",
      ],
    },
  },
  { file: "02-Architecture-Overview", src: "docs/architecture/overview.md", group: "Architecture" },
  { file: "03-Data-Flow", src: "docs/architecture/data-flow.md", group: "Architecture" },
  { file: "27-Multi-Tenancy", src: "docs/multi-tenancy.md", group: "Architecture" },
  { file: "28-Processing-Queues", src: "docs/processing-queues.md", group: "Architecture" },
  { file: "04-Pipeline-01-Upload-Intake", src: "docs/pipeline/01-upload-intake.md", group: "Pipeline" },
  { file: "31-Pipeline-01a-Bulk-Upload", src: "docs/pipeline/01a-bulk-upload.md", group: "Pipeline" },
  { file: "05-Pipeline-02-Extraction-Normalization", src: "docs/pipeline/02-extraction-normalization.md", group: "Pipeline" },
  { file: "06-Pipeline-03-Field-Validation", src: "docs/pipeline/03-field-validation.md", group: "Pipeline" },
  { file: "07-Pipeline-04-Cross-Document-Consistency", src: "docs/pipeline/04-cross-document-consistency.md", group: "Pipeline" },
  { file: "08-Pipeline-05-Issuer-Verification", src: "docs/pipeline/05-issuer-verification.md", group: "Pipeline" },
  { file: "09-Pipeline-06-Metadata-Forensics", src: "docs/pipeline/06-metadata-forensics.md", group: "Pipeline" },
  { file: "34-Pipeline-06a-Font-Consistency", src: "docs/pipeline/06a-font-consistency.md", group: "Pipeline" },
  { file: "10-Pipeline-07-ELA-Copy-Move", src: "docs/pipeline/07-ela-copy-move.md", group: "Pipeline" },
  { file: "35-Pipeline-07a-Ghost-Content", src: "docs/pipeline/07a-ghost-content.md", group: "Pipeline" },
  { file: "11-Pipeline-08-Visual-Review-AI-Generation", src: "docs/pipeline/08-visual-review-ai-generation.md", group: "Pipeline" },
  { file: "12-Pipeline-09-Signature-Verification", src: "docs/pipeline/09-signature-verification.md", group: "Pipeline" },
  { file: "13-Pipeline-10-Duplicate-Detection", src: "docs/pipeline/10-duplicate-detection.md", group: "Pipeline" },
  { file: "14-Pipeline-11-Risk-Scoring-Engine", src: "docs/pipeline/11-risk-scoring-engine.md", group: "Pipeline" },
  { file: "36-Pipeline-12-Check-Summaries", src: "docs/pipeline/12-check-summaries.md", group: "Pipeline" },
  { file: "15-Database-Schema", src: "docs/database/schema.md", group: "Database" },
  { file: "16-Database-ERD", src: "docs/database/erd.md", group: "Database" },
  { file: "17-API-Reference", src: "docs/api/README.md", group: "API" },
  { file: "24-API-Endpoint-Reference-OpenAPI", src: "docs/api/openapi.json", group: "API", openapi: true },
  { file: "18-Frontend-Screens", src: "docs/frontend/screens.md", group: "Frontend" },
  { file: "19-Frontend-Components", src: "docs/frontend/components.md", group: "Frontend" },
  { file: "20-Forensic-Report", src: "docs/reports/forensic-report.md", group: "Reports" },
  { file: "21-Local-Setup", src: "docs/deployment/local-setup.md", group: "Deployment" },
  { file: "22-Environment-Variables", src: "docs/deployment/environment-variables.md", group: "Deployment" },
  { file: "23-Azure-Migration-Notes", src: "docs/deployment/azure-migration-notes.md", group: "Deployment" },
  { file: "29-Production-Deployment", src: "docs/deployment/production-deployment.md", group: "Deployment" },
  { file: "37-Azure-Production-Runbook", src: "docs/deployment/azure-production-runbook.md", group: "Deployment" },
  { file: "25-Azure-Demo-Deployment", src: "docs/deployment/azure-demo-deployment.md", group: "Deployment" },
]
const BOOK_FILE = "FDDT-Complete-Documentation"

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const esc = (s) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")

// GitHub-style heading slug, with runs of hyphens collapsed so a link written
// either way ("a--b" / "a-b") resolves.
function slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .trim()
    .replace(/\s/g, "-")
    .replace(/-+/g, "-")
}

const bySrc = new Map(DOC_LIST.map((d) => [path.resolve(ROOT, d.src).toLowerCase(), d]))
const docId = (doc) => `doc-${doc.file.slice(0, 2)}`

// ---------------------------------------------------------------------------
// Markdown -> HTML
// ---------------------------------------------------------------------------

/**
 * Render one Markdown document.
 * mode "single": links within the doc stay live; references to other docs are
 *                kept as plain text (Chrome writes link targets as absolute
 *                file:// URLs, which would break once the PDFs are moved).
 * mode "book":   links to other docs jump inside the combined PDF; heading ids
 *                are prefixed with the doc id so they stay unique.
 */
function renderMarkdown(doc, mode) {
  const srcPath = path.resolve(ROOT, doc.src)
  const srcDir = path.dirname(srcPath)
  const prefix = mode === "book" ? `${docId(doc)}--` : ""
  const usedIds = new Map()
  const headings = []
  let title = null

  const anchor = (text) => {
    let id = prefix + slugify(text)
    const n = usedIds.get(id) ?? 0
    usedIds.set(id, n + 1)
    return n ? `${id}-${n}` : id
  }

  function resolveHref(href) {
    if (!href) return null
    if (/^(https?:|mailto:)/i.test(href)) return { href }
    const [target, frag] = href.split("#")
    if (!target) return { href: `#${prefix}${slugify(frag ?? "")}` }
    const abs = path.resolve(srcDir, decodeURIComponent(target))
    const linked = bySrc.get(abs.toLowerCase())
    if (linked) {
      if (mode === "book") {
        const p = `${docId(linked)}--`
        return { href: frag ? `#${p}${slugify(frag)}` : `#${p}top` }
      }
      if (linked === doc) return { href: frag ? `#${slugify(frag)}` : "#top" }
    }
    return null // another PDF, a source file or a folder: shown as text, not a dead link
  }

  const marked = new Marked({ gfm: true })
  marked.use({
    renderer: {
      heading(token) {
        const text = this.parser.parseInline(token.tokens)
        const id = anchor(token.text)
        if (token.depth === 1 && title === null) title = token.text.replace(/[*`_]/g, "")
        if (token.depth <= 2) headings.push({ depth: token.depth, text: token.text.replace(/[*`_]/g, ""), id })
        return `<h${token.depth} id="${id}">${text}</h${token.depth}>\n`
      },
      code(token) {
        if (token.lang === "mermaid") return `<div class="diagram"><pre class="mermaid">${esc(token.text)}</pre></div>\n`
        return `<pre class="code"><code>${esc(token.text)}</code></pre>\n`
      },
      link(token) {
        const inner = this.parser.parseInline(token.tokens)
        const r = resolveHref(token.href)
        if (!r || !r.href) return `<span class="xref">${inner}</span>`
        return `<a href="${esc(r.href)}">${inner}</a>`
      },
      image(token) {
        const abs = path.resolve(srcDir, decodeURIComponent(token.href))
        const src = existsSync(abs) ? pathToFileURL(abs).href : token.href
        return `<figure><img src="${esc(src)}" alt="${esc(token.text)}"/>${token.text ? `<figcaption>${esc(token.text)}</figcaption>` : ""}</figure>`
      },
      paragraph(token) {
        const only = token.tokens.filter((t) => !(t.type === "text" && !t.text.trim()))
        const html = this.parser.parseInline(token.tokens)
        return only.length === 1 && only[0].type === "image" ? `${html}\n` : `<p>${html}</p>\n`
      },
    },
  })

  const body = marked.parse(readFileSync(srcPath, "utf8"))
  return { title: title ?? doc.file, body, headings }
}

// ---------------------------------------------------------------------------
// OpenAPI -> HTML (24-API-Endpoint-Reference-OpenAPI)
// ---------------------------------------------------------------------------

const TAGS = [
  ["health", "Health"],
  ["auth", "Authentication"],
  ["cases", "Cases"],
  ["documents", "Documents"],
  ["case-actions", "Case actions (review decisions)"],
  ["signatures", "Signature references & matches"],
  ["audit", "Audit log"],
  ["case-reports", "Case reports"],
  ["settings", "Settings (admin)"],
]

function renderOpenApi(doc, mode) {
  const spec = JSON.parse(readFileSync(path.resolve(ROOT, doc.src), "utf8"))
  const prefix = mode === "book" ? `${docId(doc)}--` : ""
  const schemas = spec.components?.schemas ?? {}
  const modelId = (name) => `${prefix}model-${slugify(name)}`
  const md = new Marked({ gfm: true })
  const inlineMd = (s) => (s ? md.parseInline(String(s)) : "")
  const headings = []

  function typeOf(s) {
    if (!s) return "—"
    if (s.$ref) {
      const name = s.$ref.split("/").pop()
      return `<a href="#${modelId(name)}">${esc(name)}</a>`
    }
    if (s.anyOf || s.oneOf) {
      const parts = s.anyOf ?? s.oneOf
      const nonNull = parts.filter((p) => p.type !== "null")
      const nullable = nonNull.length < parts.length
      const t = nonNull.map(typeOf).join(" | ")
      return nullable ? `${t} (nullable)` : t
    }
    if (s.enum) return `${esc(s.type ?? "string")}: ${s.enum.map((v) => `<code>${esc(JSON.stringify(v))}</code>`).join(", ")}`
    if (s.type === "array") return `array of ${typeOf(s.items)}`
    if (s.type === "object" && s.additionalProperties && typeof s.additionalProperties === "object")
      return `map of ${typeOf(s.additionalProperties)}`
    if (Array.isArray(s.type)) return s.type.map(esc).join(" | ")
    const base = s.type ?? "object"
    return s.format ? `${esc(base)} (${esc(s.format)})` : esc(base)
  }

  const bodyOf = (content) => {
    if (!content) return "—"
    const [ct, media] = Object.entries(content)[0]
    return media.schema ? typeOf(media.schema) + (ct !== "application/json" ? ` <span class="muted">(${esc(ct)})</span>` : "") : esc(ct)
  }

  const ops = []
  for (const [p, methods] of Object.entries(spec.paths)) {
    for (const [m, op] of Object.entries(methods)) ops.push({ path: p, method: m.toUpperCase(), op })
  }
  const opsByTag = TAGS.map(([tag, label]) => ({ tag, label, ops: ops.filter((o) => o.op.tags?.[0] === tag) }))
  const known = new Set(TAGS.map(([t]) => t))
  const other = ops.filter((o) => !known.has(o.op.tags?.[0]))
  if (other.length) opsByTag.push({ tag: "other", label: "Other", ops: other })
  const opId = (o) => `${prefix}op-${slugify(`${o.method} ${o.path}`)}`

  let h = ""
  const h1id = `${prefix}api-endpoint-reference`
  headings.push({ depth: 1, text: "API endpoint reference", id: h1id })
  h += `<h1 id="${h1id}">API endpoint reference</h1>`
  h += `<p class="lead">${esc(spec.info.title)} · version ${esc(spec.info.version)} · OpenAPI ${esc(spec.openapi)}</p>`
  h += `<p>${ops.length} operations across ${Object.keys(spec.paths).length} paths and ${Object.keys(schemas).length} data models. This reference is generated from the API definition itself (<span class="xref">docs/api/openapi.json</span>).</p>`
  const convId = `${prefix}conventions`
  headings.push({ depth: 2, text: "Conventions", id: convId })
  h += `<h2 id="${convId}">Conventions</h2><ul>
    <li><b>Authentication:</b> endpoints marked <span class="chip auth">Bearer JWT</span> need the header <code>Authorization: Bearer &lt;token&gt;</code>. Obtain a token from <code>POST /auth/login</code> (email and password).</li>
    <li><b>Roles</b> (ranked, each includes everything below it): <code>user</code> (submitter) &lt; <code>reviewer_l1</code> &lt; <code>reviewer_l2</code> &lt; <code>admin</code>. "Reviewer/admin" means <code>reviewer_l1</code> or above. On a case escalated to L2 (<code>assigned_tier</code> = <code>l2</code>) only <code>reviewer_l2</code> and <code>admin</code> may approve, reject or escalate; a <code>reviewer_l1</code> gets 403 there but can still read the case. Role rules for each endpoint are stated in its description.</li>
    <li><b>Errors:</b> every operation lists its documented error responses. 422 is a request-validation error.</li>
    <li><b>Formats:</b> requests and responses are JSON (file uploads are <code>multipart/form-data</code>); identifiers are UUIDs; timestamps are ISO 8601.</li>
  </ul>`

  const idxId = `${prefix}endpoint-index`
  headings.push({ depth: 2, text: "Endpoint index", id: idxId })
  h += `<h2 id="${idxId}">Endpoint index</h2><table class="index"><thead><tr><th>Method</th><th>Path</th><th>Summary</th></tr></thead><tbody>`
  for (const g of opsByTag) {
    if (!g.ops.length) continue
    h += `<tr class="group"><td colspan="3">${esc(g.label)}</td></tr>`
    for (const o of g.ops)
      h += `<tr><td><span class="method m-${o.method.toLowerCase()}">${o.method}</span></td><td><a href="#${opId(o)}"><code>${esc(o.path)}</code></a></td><td>${esc(o.op.summary ?? "")}</td></tr>`
  }
  h += `</tbody></table>`

  for (const g of opsByTag) {
    if (!g.ops.length) continue
    const gid = `${prefix}tag-${slugify(g.tag)}`
    headings.push({ depth: 2, text: g.label, id: gid })
    h += `<h2 id="${gid}">${esc(g.label)}</h2>`
    for (const o of g.ops) {
      const { op } = o
      const secured = op.security?.length > 0
      h += `<section class="op" id="${opId(o)}">`
      h += `<div class="op-head"><span class="method m-${o.method.toLowerCase()}">${o.method}</span> <code class="path">${esc(o.path)}</code></div>`
      h += `<div class="op-sum">${esc(op.summary ?? "")} <span class="chip ${secured ? "auth" : "public"}">${secured ? "Bearer JWT" : "Public"}</span></div>`
      if (op.description) h += `<div class="op-desc">${md.parse(op.description)}</div>`
      if (op.parameters?.length) {
        h += `<h4>Parameters</h4><table><thead><tr><th>Name</th><th>In</th><th>Type</th><th>Required</th><th>Description</th></tr></thead><tbody>`
        for (const p of op.parameters)
          h += `<tr><td><code>${esc(p.name)}</code></td><td>${esc(p.in)}</td><td>${typeOf(p.schema)}</td><td>${p.required ? "yes" : "no"}</td><td>${inlineMd(p.description ?? p.schema?.description)}</td></tr>`
        h += `</tbody></table>`
      }
      if (op.requestBody) {
        h += `<h4>Request body${op.requestBody.required ? "" : " (optional)"}</h4><p>${bodyOf(op.requestBody.content)}</p>`
      }
      h += `<h4>Responses</h4><table><thead><tr><th>Status</th><th>Description</th><th>Body</th></tr></thead><tbody>`
      for (const [code, r] of Object.entries(op.responses ?? {}))
        h += `<tr><td><code>${esc(code)}</code></td><td>${inlineMd(r.description)}</td><td>${bodyOf(r.content)}</td></tr>`
      h += `</tbody></table></section>`
    }
  }

  const modelsId = `${prefix}data-models`
  headings.push({ depth: 2, text: "Data models", id: modelsId })
  h += `<h2 id="${modelsId}">Data models</h2>`
  for (const name of Object.keys(schemas).sort((a, b) => a.localeCompare(b))) {
    const s = schemas[name]
    h += `<section class="model"><h3 id="${modelId(name)}">${esc(name)}</h3>`
    if (s.description) h += `<div class="op-desc">${md.parse(s.description)}</div>`
    if (s.enum) {
      h += `<p>Allowed values: ${s.enum.map((v) => `<code>${esc(JSON.stringify(v))}</code>`).join(", ")}</p>`
    } else if (s.properties) {
      const req = new Set(s.required ?? [])
      h += `<table><thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr></thead><tbody>`
      for (const [f, fs] of Object.entries(s.properties))
        h += `<tr><td><code>${esc(f)}</code></td><td>${typeOf(fs)}</td><td>${req.has(f) ? "yes" : "no"}</td><td>${inlineMd(fs.description)}</td></tr>`
      h += `</tbody></table>`
    } else {
      h += `<p>${typeOf(s)}</p>`
    }
    h += `</section>`
  }
  return { title: "API endpoint reference", body: h, headings }
}

const render = (doc, mode) => (doc.openapi ? renderOpenApi(doc, mode) : renderMarkdown(doc, mode))

// ---------------------------------------------------------------------------
// Page assembly
// ---------------------------------------------------------------------------

const CSS = `
@page { size: A4; margin: 18mm 16mm 16mm 16mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; font-size: 9.6pt; line-height: 1.5; color: #1f2937; margin: 0; }
h1, h2, h3, h4 { color: #0b2545; line-height: 1.25; break-after: avoid; page-break-after: avoid; }
h1 { font-size: 22pt; margin: 0 0 12pt; padding-bottom: 6pt; border-bottom: 1.5pt solid #0b2545; }
h2 { font-size: 14pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 0.6pt solid #d7dee8; }
h3 { font-size: 11.5pt; margin: 14pt 0 4pt; }
h4 { font-size: 10pt; margin: 10pt 0 4pt; }
p { margin: 0 0 7pt; }
ul, ol { margin: 0 0 8pt; padding-left: 18pt; }
li { margin: 0 0 2.5pt; }
a { color: #1d4ed8; text-decoration: none; }
.xref { color: inherit; }
code { font-family: Consolas, "Cascadia Mono", Menlo, monospace; font-size: 8.6pt; background: #f1f4f9; padding: 0.5pt 3pt; border-radius: 2.5pt; color: #0f2a4a; }
pre.code { font-family: Consolas, "Cascadia Mono", Menlo, monospace; font-size: 8.2pt; background: #f6f8fb; border: 0.6pt solid #dde3ec; border-radius: 4pt; padding: 7pt 9pt; white-space: pre-wrap; word-break: break-word; break-inside: avoid; }
pre.code code { background: none; padding: 0; font-size: inherit; }
blockquote { margin: 6pt 0 10pt; padding: 6pt 10pt; background: #fffbeb; border-left: 3pt solid #f59e0b; color: #422006; break-inside: avoid; }
blockquote p:last-child { margin-bottom: 0; }
table { width: 100%; border-collapse: collapse; margin: 4pt 0 10pt; font-size: 8.6pt; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th { background: #e8eef7; color: #0b2545; text-align: left; font-weight: 600; }
th, td { border: 0.6pt solid #d7dee8; padding: 4pt 6pt; vertical-align: top; word-break: break-word; }
td code { word-break: break-all; }
hr { border: none; border-top: 0.6pt solid #d7dee8; margin: 12pt 0; }
figure { margin: 8pt 0 12pt; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; max-height: 120mm; border: 0.6pt solid #cbd5e1; border-radius: 3pt; }
figcaption { font-size: 8pt; font-style: italic; color: #64748b; margin-top: 4pt; }
.diagram { margin: 6pt 0 12pt; padding: 8pt; border: 0.6pt solid #e2e8f0; border-radius: 4pt; text-align: center; break-inside: avoid; }
.diagram svg { max-width: 100%; height: auto; }
pre.mermaid { font-family: inherit; background: none; border: none; margin: 0; }
.doc { break-before: page; }
.doc:first-of-type { break-before: auto; }
.lead { color: #475569; }
.muted { color: #64748b; }
/* cover + contents */
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; break-after: page; }
.cover.center { align-items: center; text-align: center; }
.cover .t1 { font-size: 30pt; font-weight: 700; color: #0b2545; line-height: 1.15; }
.cover .t2 { font-size: 15pt; color: #64748b; margin-top: 2pt; }
.cover .t3 { font-size: 14pt; color: #1d4ed8; margin-top: 12pt; }
.cover .rule { width: 40%; border-top: 1.5pt solid #1d4ed8; margin: 20pt 0 12pt; }
.cover .meta { font-size: 9.5pt; color: #64748b; margin: 2pt 0; }
.cover .note { font-size: 9pt; font-style: italic; color: #64748b; margin-top: 50pt; max-width: 130mm; }
.contents { break-after: page; }
.contents h2 { border: none; font-size: 16pt; }
.contents .grp { font-weight: 700; color: #0b2545; margin: 8pt 0 2pt; font-size: 10pt; }
.contents .ent { margin: 1.5pt 0 1.5pt 12pt; }
.contents .sub { margin: 1pt 0 1pt 26pt; font-size: 9pt; }
.contents a { color: #1f2937; }
/* OpenAPI */
.method { display: inline-block; min-width: 38pt; text-align: center; font-size: 7.5pt; font-weight: 700; color: #fff; border-radius: 3pt; padding: 1.5pt 5pt; letter-spacing: 0.3pt; }
.m-get { background: #2563eb; } .m-post { background: #15803d; } .m-patch { background: #c2410c; } .m-put { background: #b45309; } .m-delete { background: #b91c1c; }
table.index tr.group td { background: #f1f4f9; font-weight: 700; color: #0b2545; }
.op { margin: 10pt 0 14pt; }
.op-head { font-size: 11pt; margin-bottom: 3pt; break-after: avoid; }
.op-head .path { font-size: 10pt; background: none; font-weight: 600; }
.op-sum { font-weight: 600; color: #0b2545; margin-bottom: 4pt; break-after: avoid; }
.op-desc p { margin-bottom: 5pt; }
.chip { display: inline-block; font-size: 7.3pt; font-weight: 600; border-radius: 3pt; padding: 0.5pt 5pt; margin-left: 4pt; vertical-align: 1pt; }
.chip.auth { background: #eef2ff; color: #3730a3; border: 0.6pt solid #c7d2fe; }
.chip.public { background: #ecfdf5; color: #047857; border: 0.6pt solid #a7f3d0; }
.model { break-inside: avoid; }
`

function footerTemplate(title) {
  return `<div style="font-family:'Segoe UI',Arial,sans-serif;font-size:7px;color:#555;width:100%;padding:0 16mm;display:flex;justify-content:space-between;">
    <span>${esc(PRODUCT)} · ${esc(title)}</span>
    <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>`
}

function coverHtml(c) {
  return `<section class="cover center">
    <div class="t1">${esc(c.title)}</div>
    ${c.tag ? `<div class="t2">${esc(c.tag)}</div>` : ""}
    <div class="t3">${esc(c.subtitle)}</div>
    <div class="rule"></div>
    ${(c.meta ?? []).map((m) => `<div class="meta">${esc(m)}</div>`).join("")}
    ${c.note ? `<div class="note">${esc(c.note)}</div>` : ""}
  </section>`
}

// Warn about in-document links whose target id doesn't exist (a renamed
// heading) — Chrome silently drops those links.
function checkAnchors(name, html) {
  const ids = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]))
  const missing = [...new Set([...html.matchAll(/href="#([^"]+)"/g)].map((m) => m[1]))].filter((id) => !ids.has(id))
  for (const id of missing) console.warn(`  warning: ${name}: link to missing anchor #${id}`)
}

function pageHtml(title, inner) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${esc(title)}</title><style>${CSS}</style></head><body>${inner}</body></html>`
}

function singleHtml(doc) {
  const r = render(doc, "single")
  let inner = `<a id="top"></a>`
  if (doc.cover) {
    inner += coverHtml(doc.cover)
    const h2 = r.headings.filter((x) => x.depth === 2)
    inner += `<section class="contents"><h2>Contents</h2>${h2.map((x) => `<div class="ent"><a href="#${x.id}">${esc(x.text)}</a></div>`).join("")}</section>`
  }
  inner += `<article>${r.body}</article>`
  return { title: r.title, html: pageHtml(r.title, inner) }
}

function bookHtml() {
  const rendered = DOC_LIST.map((doc) => ({ doc, r: render(doc, "book") }))
  let inner = `<section class="cover"><div class="t1">FDDT</div><div class="t3" style="color:#475569;font-size:12pt;margin-top:8pt">${esc("Fraud Document Detection Tool")}</div><div class="meta">Complete project documentation</div></section>`
  inner += `<section class="contents"><h2>Contents</h2>`
  let group = null
  for (const { doc, r } of rendered) {
    if (doc.group !== group) {
      group = doc.group
      inner += `<div class="grp">${esc(group)}</div>`
    }
    inner += `<div class="ent"><a href="#${docId(doc)}--top">${esc(r.title)}</a></div>`
  }
  inner += `</section>`
  for (const { doc, r } of rendered) inner += `<article class="doc" id="${docId(doc)}--top">${r.body}</article>`
  return { title: "Complete Documentation", html: pageHtml("FDDT Documentation", inner) }
}

// ---------------------------------------------------------------------------
// Printing
// ---------------------------------------------------------------------------

function findBrowser() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
  ].filter(Boolean)
  const found = candidates.find((p) => existsSync(p))
  if (!found) throw new Error("No Chrome/Edge found. Set CHROME_PATH to a Chromium-based browser binary.")
  return found
}

async function printPdf(browser, name, title, html) {
  checkAnchors(name, html)
  const htmlPath = path.join(TMP_DIR, `${name}.html`)
  writeFileSync(htmlPath, html, "utf8")
  const page = await browser.newPage()
  try {
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" })
    if (html.includes('class="mermaid"')) {
      await page.addScriptTag({ path: MERMAID_JS })
      await page.evaluate(async () => {
        // eslint-disable-next-line no-undef
        mermaid.initialize({ startOnLoad: false, theme: "neutral", securityLevel: "loose", flowchart: { htmlLabels: true } })
        // eslint-disable-next-line no-undef
        await mermaid.run({ querySelector: ".mermaid" })
        // Fit every diagram inside one page (the A4 content box is ~178 x 263 mm).
        const maxH = (240 / 25.4) * 96
        for (const svg of document.querySelectorAll(".diagram svg")) {
          svg.style.maxWidth = "100%"
          svg.removeAttribute("height")
          svg.setAttribute("width", "100%")
          const box = svg.getBoundingClientRect()
          if (box.height > maxH) {
            svg.setAttribute("width", `${(box.width * maxH) / box.height}px`)
          }
        }
      })
    }
    await page.evaluate(() => document.fonts.ready)
    await page.pdf({
      path: path.join(OUT_DIR, `${name}.pdf`),
      format: "A4",
      printBackground: true,
      preferCSSPageSize: true,
      displayHeaderFooter: true,
      headerTemplate: "<span></span>",
      footerTemplate: footerTemplate(title),
      outline: true,
      tagged: true,
    })
  } finally {
    await page.close()
  }
}

async function main() {
  const only = []
  for (let i = 2; i < process.argv.length; i++) if (process.argv[i] === "--only") only.push(process.argv[++i])
  const want = (name) => !only.length || only.some((p) => name.startsWith(p))

  rmSync(TMP_DIR, { recursive: true, force: true })
  mkdirSync(TMP_DIR, { recursive: true })
  mkdirSync(OUT_DIR, { recursive: true })

  const browser = await puppeteer.launch({ executablePath: findBrowser(), headless: true, args: ["--allow-file-access-from-files"] })
  try {
    for (const doc of DOC_LIST) {
      if (!want(doc.file)) continue
      const { title, html } = singleHtml(doc)
      await printPdf(browser, doc.file, title, html)
      console.log(`wrote docs-pdf/${doc.file}.pdf`)
    }
    if (want(BOOK_FILE)) {
      const { title, html } = bookHtml()
      await printPdf(browser, BOOK_FILE, title, html)
      console.log(`wrote docs-pdf/${BOOK_FILE}.pdf`)
    }
  } finally {
    await browser.close()
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
