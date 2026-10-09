# 06b — Spelling (English)

**What it detects:** misspelled **ordinary English words** in a document's printed text — "Certifcate",
"Univeristy", "Recieved". Genuine issuers print from proofread templates; a retyped or
fabricated document often carries a typo. Small issuers make mistakes too, so this is a **weak signal**:
every confirmed misspelling is shown (with its box on the page), but the rule that scores it is low-weight.

**Code:** `services/forensics/spelling.py` (`analyze_spelling`), run by `tasks/document_processing.py`
(`_run_spelling_check`, right after font consistency — it needs that task's OCR words and extracted fields).
Every document, PDF or image. One `document_checks` row: `spelling_consistency`. Dictionaries:
`pyspellchecker` (word-frequency lists, bundled — no network).

**English only.** An Arabic pass was built and tried on real documents, but its results were not good
enough, so Arabic words are skipped (`CHECKED_LANGUAGES = (ENGLISH,)` in `spelling.py`; adding `ARABIC`
turns it back on). Language is decided **per word**, so a mixed English / Arabic document still has its
English words checked; the text-layer agreement test below is also computed from English words only.

## How noise is kept out

Two passes. The dictionary pass is cheap and strict about what it lets through; the language model
judges only what survives.

### 1. Dictionary pass — what becomes a candidate

An OCR word is checked only if it is:

| Filter | Why |
|---|---|
| Latin letters only (Arabic and mixed-script words skipped); no digits or symbols (`@ # / . _ …`) | codes, IDs, emails, amounts |
| ≥ 4 letters; not an acronym (all caps ≤ 5); not CamelCase | abbreviations, brands |
| not in an extracted field that is a name, address, ID, contact detail or code (`student_name`, `address`, `iban`, `trn`, …) or the reference number | names are not dictionary words |
| not handwriting (OCR styleFont), not ≥ 50 % inside a detected signature or stamp | signature ink, curved stamp text |
| OCR confidence ≥ 0.85 | an uncertain read is more likely an OCR error |

It becomes a **candidate** only if it is:

- **unknown in every accepted variant** — British spellings (`colour`, `organisation`, `programme`,
  `enrolment`…) and a small regional allow-list (`Sharjah`, `IGCSE`, `KHDA`…);
- **in the PDF's own text layer as read**, on a page whose text layer agrees with OCR on ≥ 60 % of its
  words (else the layer is not trusted). A word the PDF spells correctly but OCR misread is dropped
  here. Scans and images have no layer — only the OCR-confidence filter applies;
- **one edit away from a common word** (frequency ≥ 100). An unknown word with
  no near common word is a name or a foreign word, not a typo.

At most 30 distinct candidates (most frequent first) go on to the model.

### 2. Language-model judgment

One call per document (`LLMService.judge_spelling`, temperature 0), each candidate with ±6 words of
context and its dictionary suggestions. Verdicts: `misspelling`, `valid_word`, `valid_variant`,
`proper_noun`, `abbreviation_or_code`, `transliteration_or_foreign`, `ocr_misread`, `unsure`. The prompt
tells it that a false alarm is worse than a miss.

**Flagged:** only `misspelling` at **medium or high** confidence, with a correction that differs from the
word. Everything else is listed in the scope note (`data.set_aside`), not scored.

**No model configured:** nothing is flagged; result `limited`, the unconfirmed words noted. The dictionary
alone is too noisy to score.

## Result

```
{"result": "flag" | "pass" | "limited" | "not_applicable",
 "details": [
   {"finding": "spelling_error", "severity": "low", "page": 1,
    "bounding_box": {...}, "description": "Page 1: 'Certifcate' looks misspelled — 'Certificate' (English). …",
    "data": {"text", "correction", "language": "en", "context", "model_confidence",
             "ocr_confidence", "confirmed_by_text_layer"}},
   …one per occurrence…,
   {"finding": "spelling_scope", "severity": "info",
    "data": {"words_checked", "words_checked_by_language", "unknown_words", "candidates",
             "text_layer_pages", "set_aside": [{"word", "verdict"}], "misspellings"}}]}
```

`not_applicable`: no printed English word was read (e.g. an Arabic-only document).

## Scoring

Rule `content.spelling_error` (migration `a2d6f9c3e7b4`): any `spelling_error` finding → **weight 5,
severity low**, fired **once per document** however many words (one typo is shown, but ten typos do not
add up to a high score). Editable in Settings › Risk Rules like any rule.

## Where it shows

- Case page: check "Spelling (English)" — headline "2 misspelled words: 'Certifcate', 'Univeristy'",
  one item per word with its correction; a **sky-blue box** around each occurrence on the page.
- Report: in the checks table, as a highlight region (sky blue) and as an exception line per word.
