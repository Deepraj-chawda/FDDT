import { pdfjs } from "react-pdf"
// Vite's `?url` import gives the worker script's built URL rather than its
// source — pdf.js does its actual parsing off the main thread in this worker,
// and react-pdf needs GlobalWorkerOptions.workerSrc set before rendering.
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url"

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

// Where pdf.js loads its decoders and data from — served at /pdfjs/ by the
// `pdfjsAssets` plugin in vite.config.ts. Without them pdf.js does not fail,
// it silently drops what it cannot decode: scanner output with CCITT or JBIG2
// text layers (decoded by jbig2.wasm) renders as a blank white page, JPEG 2000
// images (openjpeg.wasm) disappear, and non-embedded standard fonts, CJK
// character maps and ICC colours are substituted.
const base = `${import.meta.env.BASE_URL}pdfjs/`

// One stable object: react-pdf reloads the document whenever `options`
// changes identity.
export const PDF_DOCUMENT_OPTIONS = {
  wasmUrl: `${base}wasm/`,
  cMapUrl: `${base}cmaps/`,
  cMapPacked: true,
  standardFontDataUrl: `${base}standard_fonts/`,
  iccUrl: `${base}iccs/`,
}
