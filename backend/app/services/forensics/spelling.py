"""
Spelling check: misspelled ordinary English words in the document's
printed text.

English only (CHECKED_LANGUAGES): on real documents the Arabic pass was not
reliable enough — Arabic words are skipped. The Arabic normalization below is
kept, and still used to match words against extracted values and the text
layer; adding ARABIC to CHECKED_LANGUAGES turns the Arabic pass back on.

Genuine issuers print from proofread templates; a retyped or fabricated
document often carries a typo ("Certifcate", "Univeristy", "Recieved"). It
is a weak signal on its own (small issuers make mistakes too), so the rule
that scores it is low-weight — but every confirmed misspelling is shown,
with its box on the page.

Two passes, built to keep false alarms out:

1. Dictionary pass (pyspellchecker's word lists) over the
   OCR words. A word only becomes a candidate if it survives every filter:
     - not part of a name, address, ID, email, phone or code — words in the
       extracted values of such fields are skipped, and so are tokens with
       digits or symbols, short words, acronyms and CamelCase;
     - not handwriting, and not inside a detected signature or stamp;
     - read confidently by OCR (an uncertain read is more likely an OCR
       error than a typo);
     - on a page with a usable text layer (a digital PDF), present in that
       text layer as read — a word the PDF itself spells correctly was
       misread by OCR, not mistyped;
     - in a checked language (CHECKED_LANGUAGES: English);
     - unknown to the dictionary in every accepted variant: British
       spellings (colour, organisation) and, in Arabic, the hamza forms,
       ة/ه, ى/ي, diacritics and tatweel, and the attached prefixes and
       suffixes (و ب ل ف ك ال ... ة ها هم ...);
     - one edit away from a COMMON dictionary word — an unknown word with no
       near common word is a name or a foreign word, not a typo.
2. A language-model judgment of the remaining candidates (one call per
   document), with the text around each: misspelling, or a valid word /
   variant, a proper noun, an abbreviation, a transliteration, an OCR
   misread. Only "misspelling" at medium or high confidence is flagged.
   Without a configured model nothing is flagged (the candidates are only
   noted): the dictionary alone is too noisy to score.

Runs inside app/tasks/document_processing.py (it needs that task's OCR
words and extracted fields), for every document type, PDF or image.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any, Callable

from app.services.ocr_service import OCRPage, OCRWord

ENGLISH, ARABIC = "en", "ar"
# The languages whose words are checked; words in any other are skipped.
CHECKED_LANGUAGES: tuple[str, ...] = (ENGLISH,)
_LANGUAGE_NAMES = {ENGLISH: "English", ARABIC: "Arabic"}

# OCR word confidence below which a word is not checked (Arabic OCR is the
# weaker of the two, so it must be surer).
_MIN_OCR_CONFIDENCE = {ENGLISH: 0.85, ARABIC: 0.9}
# Shortest word checked: short words are mostly abbreviations or particles.
_MIN_LENGTH = {ENGLISH: 4, ARABIC: 3}
# A suggestion must be at least this common in the language's frequency list
# (pyspellchecker's floor is 50 for English) — "near a rare word" is how names look.
_MIN_SUGGESTION_FREQUENCY = {ENGLISH: 100, ARABIC: 20}
# At most this many distinct words are sent to the model per document.
_MAX_CANDIDATES = 30
# The model's confidence a misspelling needs to be flagged.
_FLAG_CONFIDENCE = ("medium", "high")
# A text layer is only trusted to rule out OCR errors when it agrees with
# OCR on most of the page's words (else it is broken or a different layer).
_TEXT_LAYER_AGREEMENT = 0.6
_CONTEXT_WORDS = 6

# Fields whose values are names, addresses, identifiers or contact details —
# not dictionary words, so their words are not checked.
_SKIP_FIELD_RE = re.compile(
    r"name|student|customer|client|recipient|payer|payee|bill_?to|ship_?to|parent|guardian|signatory|"
    r"address|street|city|area|emirate|country|location|p\.?o|box|email|e_mail|phone|mobile|tel|fax|"
    r"website|url|iban|account|swift|bic|trn|vat_?(?:no|number|reg)|tax_?(?:id|no|number|reg)|"
    r"\bid\b|_id|id_|number|no\b|code|reference|passport|licen[cs]e|registration|bank",
    re.IGNORECASE,
)
# Accepted words a general dictionary lacks: regional names and terms seen
# on this platform's documents.
_ENGLISH_ALLOWED = {
    "bonafide", "sharjah", "ajman", "fujairah", "khaimah", "quwain", "dhabi", "emirati", "dirhams",
    "igcse", "gcse", "cbse", "icse", "ielts", "toefl", "khda", "adek", "spea", "llc", "fzco", "fze", "pjsc",
    "dmcc", "jafza", "trn", "iban", "aed", "qty", "nos", "kindergarten", "programme", "programmes",
    "enrolment", "enrolments", "cheque", "cheques", "licence", "centre", "centres", "behaviour",
    "learnt", "spelt", "whilst", "amongst", "fulfil", "instalment", "instalments", "storey",
}
# British -> American rewrites, tried on an unknown English word.
_BRITISH = (
    ("isation", "ization"), ("ising", "izing"), ("ised", "ized"), ("ise", "ize"), ("yse", "yze"),
    ("our", "or"), ("tre", "ter"), ("ogue", "og"), ("lled", "led"), ("lling", "ling"), ("ence", "ense"),
    ("mme", "m"), ("ae", "e"), ("oe", "e"),
)

_ARABIC_DIACRITICS_RE = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال", "و", "ف", "ب", "ل", "ك", "س")
_ARABIC_SUFFIXES = ("كما", "هما", "تها", "ات", "ان", "ين", "ون", "ها", "هم", "هن", "كم", "كن", "نا", "ية", "ه", "ة", "ي", "ك", "ا", "ت")
_EDGE_PUNCTUATION = "\"'`.,:;!?()[]{}<>«»“”‘’،؛؟*•·-–—/\\|_"


def _script(token: str) -> str | None:
    """ENGLISH for all-Latin letters, ARABIC for all-Arabic, else None."""
    latin = arabic = 0
    for ch in token:
        if "a" <= ch.lower() <= "z":
            latin += 1
        elif "ء" <= ch <= "ي" or ch in "ٱپچڤگیک":
            arabic += 1
        elif ch in "'’" or unicodedata.category(ch).startswith("M") or ch == "ـ":
            continue
        else:
            return None
    if latin and not arabic:
        return ENGLISH
    if arabic and not latin:
        return ARABIC
    return None


def normalize_arabic(word: str) -> str:
    """Diacritics and tatweel removed, hamza forms of alif unified."""
    word = unicodedata.normalize("NFKC", word)
    word = _ARABIC_DIACRITICS_RE.sub("", word)
    return re.sub("[أإآٱ]", "ا", word)


def _arabic_variants(word: str) -> set[str]:
    """`word` (normalized) in every accepted spelling: ة/ه and ى/ي at the end,
    hamza seats, and with the attached prefixes/suffixes taken off."""
    base = normalize_arabic(word)
    forms = {base, word}
    if base.endswith(("ة", "ه")):
        forms |= {base[:-1] + "ة", base[:-1] + "ه"}
    if base.endswith(("ى", "ي")):
        forms |= {base[:-1] + "ى", base[:-1] + "ي"}
    forms |= {f.replace("ؤ", "و").replace("ئ", "ي") for f in list(forms)}
    stems = set()
    for form in forms:
        for prefix in ("",) + _ARABIC_PREFIXES:
            if prefix and not form.startswith(prefix):
                continue
            stem = form[len(prefix):]
            for suffix in ("",) + _ARABIC_SUFFIXES:
                if suffix and not stem.endswith(suffix):
                    continue
                core = stem[: len(stem) - len(suffix)] if suffix else stem
                # Two-letter stems are mostly particles: they would make almost
                # anything "known" once a prefix and suffix are taken off.
                if len(core) >= 3:
                    stems.add(core)
    return forms | stems


def _english_variants(word: str) -> set[str]:
    forms = {word}
    for british, american in _BRITISH:
        if british in word:
            forms.add(word.replace(british, american))
    if word.endswith("'s"):
        forms.add(word[:-2])
    return forms


@lru_cache(maxsize=2)
def _dictionary(language: str):
    from spellchecker import SpellChecker

    return SpellChecker(language=language, distance=1)


def _known(language: str, word: str) -> bool:
    spell = _dictionary(language)
    if language == ENGLISH:
        return word in _ENGLISH_ALLOWED or any(v in spell for v in _english_variants(word))
    return any(v in spell for v in _arabic_variants(word))


def _suggestions(language: str, word: str) -> list[str]:
    """Common dictionary words one edit from `word`, most frequent first —
    accepted-variant look-alikes removed (they are not corrections)."""
    spell = _dictionary(language)
    target = word if language == ENGLISH else normalize_arabic(word)
    found = spell.known(spell.edit_distance_1(target))
    minimum = _MIN_SUGGESTION_FREQUENCY[language]
    if language == ARABIC:
        variants = _arabic_variants(word)
        found = {s for s in found if not (_arabic_variants(s) & variants)}
    ranked = sorted(
        (s for s in found if (spell.word_frequency[s] or 0) >= minimum),
        key=lambda s: -(spell.word_frequency[s] or 0),
    )
    return ranked[:3]


def _clean_token(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip(_EDGE_PUNCTUATION + " ")


def _check_form(token: str, language: str) -> str | None:
    """The form of `token` to look up, or None if it is not a word to check
    (acronym, CamelCase, too short, digits/symbols)."""
    if any(ch.isdigit() for ch in token) or re.search(r"[@#&%$+=/\\._]", token):
        return None
    if language == ARABIC:
        word = normalize_arabic(token)
        return word if len(word) >= _MIN_LENGTH[ARABIC] else None
    word = token.replace("’", "'")
    if "'" in word and not word.lower().endswith("'s"):
        return None
    letters = word.replace("'", "")
    if len(letters) < _MIN_LENGTH[ENGLISH]:
        return None
    if letters.isupper():
        # Acronyms are short; a long all-caps word is a heading — checked.
        return word.lower() if len(letters) > 5 else None
    if any(ch.isupper() for ch in letters[1:]):
        return None  # CamelCase: a brand or a code
    return word.lower()


def _skipped_words(extracted_fields: dict[str, Any] | None) -> set[str]:
    """Words of the extracted values that are names, addresses, IDs, contact
    details (normalized for lookup)."""
    words: set[str] = set()
    fields = extracted_fields or {}
    values: list[str] = []
    for item in fields.get("additional_fields") or []:
        if isinstance(item, dict) and _SKIP_FIELD_RE.search(str(item.get("field_name") or "")):
            values += [str(item.get("value") or ""), str(item.get("value_as_read") or "")]
    reference = ((fields.get("core_fields") or {}).get("reference_number") or {}).get("value")
    if reference:
        values.append(str(reference))
    for value in values:
        for raw in value.split():
            token = _clean_token(raw)
            if token:
                words.add(normalize_arabic(token).casefold())
    return words


def _box(word: OCRWord, page: int, pad: float = 0.002) -> dict[str, Any]:
    x, y = max(word.x - pad, 0.0), max(word.y - pad, 0.0)
    return {
        "page": page,
        "x": round(x, 5),
        "y": round(y, 5),
        "width": round(min(word.width + 2 * pad, 1.0 - x), 5),
        "height": round(min(word.height + 2 * pad, 1.0 - y), 5),
    }


def _in_regions(word: OCRWord, page: int, regions: list[dict[str, Any]]) -> bool:
    """Whether most of `word` lies inside a detected signature or stamp."""
    area = word.width * word.height
    if area <= 0:
        return False
    for region in regions:
        box = region.get("bounding_box") if isinstance(region, dict) else None
        if not isinstance(box, dict) or int(box.get("page") or 1) != page:
            continue
        try:
            w = min(word.x + word.width, box["x"] + box["width"]) - max(word.x, box["x"])
            h = min(word.y + word.height, box["y"] + box["height"]) - max(word.y, box["y"])
        except (KeyError, TypeError):
            continue
        if w > 0 and h > 0 and w * h / area >= 0.5:
            return True
    return False


def _lookup_key(token: str) -> str:
    return normalize_arabic(_clean_token(token)).casefold()


def text_layer_words(pdf_bytes: bytes | None) -> dict[int, set[str]]:
    """Page number -> the words of the PDF's own text layer (lookup keys,
    each also reversed: some PDFs store Arabic in visual order)."""
    if not pdf_bytes:
        return {}
    import pymupdf

    pages: dict[int, set[str]] = {}
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                words: set[str] = set()
                for item in page.get_text("words"):
                    for raw in re.split(r"[\s\-–—/]+", str(item[4])):
                        key = _lookup_key(raw)
                        if key:
                            words |= {key, key[::-1]}
                pages[index + 1] = words
    except Exception:  # noqa: BLE001 - no text layer to compare against is not an error
        return {}
    return pages


def _usable_layers(ocr_pages: list[OCRPage], layers: dict[int, set[str]]) -> dict[tuple[int, str], set[str]]:
    """(page, language) -> text-layer words, where the layer agrees with OCR on
    most of that page's words in that language."""
    usable: dict[tuple[int, str], set[str]] = {}
    for page in ocr_pages:
        layer = layers.get(page.page_number)
        if not layer:
            continue
        for language in CHECKED_LANGUAGES:
            keys = [
                _lookup_key(w.text) for w in page.words
                if _script(_clean_token(w.text)) == language and len(_clean_token(w.text)) >= 3
            ]
            if keys and sum(k in layer for k in keys) / len(keys) >= _TEXT_LAYER_AGREEMENT:
                usable[(page.page_number, language)] = layer
    return usable


def _languages() -> str:
    return " and ".join(_LANGUAGE_NAMES[language] for language in CHECKED_LANGUAGES)


def _scope(result: str, data: dict[str, Any]) -> dict[str, Any]:
    checked = data.get("words_checked", 0)
    if result == "not_applicable":
        description = f"No printed {_languages()} text was read from this document."
    else:
        description = (
            f"{checked} printed word(s) checked against the {_languages()} dictionary; "
            f"{data.get('candidates', 0)} unknown word(s) close to a common word were judged by the language model."
        )
    return {"finding": "spelling_scope", "severity": "info", "description": description, "data": data}


Judge = Callable[[str, list[dict[str, Any]]], Any]


def analyze_spelling(
    ocr_pages: list[OCRPage],
    *,
    extracted_fields: dict[str, Any] | None = None,
    document_type: str | None = None,
    pdf_bytes: bytes | None = None,
    signature_regions: list[dict[str, Any]] | None = None,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """{"result": "pass"|"flag"|"limited"|"not_applicable", "details": [...]}
    for a `document_checks.result` column. `judge` is the language model's
    judge_spelling (None when no model is configured)."""
    skipped = _skipped_words(extracted_fields)
    layers = _usable_layers(ocr_pages, text_layer_words(pdf_bytes))
    regions = [r for r in signature_regions or [] if isinstance(r, dict)]

    checked = {language: 0 for language in CHECKED_LANGUAGES}
    # lookup form -> its occurrences [(page, word index, OCRWord, token)]
    unknown: dict[tuple[str, str], list[tuple[OCRPage, int, OCRWord, str]]] = {}
    for page in ocr_pages:
        for index, word in enumerate(page.words):
            if word.italic_or_handwritten or _in_regions(word, page.page_number, regions):
                continue
            for token in re.split(r"[\-–—/]+", _clean_token(word.text)):
                token = _clean_token(token)
                language = _script(token) if token else None
                if language not in CHECKED_LANGUAGES:
                    continue
                form = _check_form(token, language) if language else None
                if form is None or _lookup_key(token) in skipped:
                    continue
                if word.confidence is not None and word.confidence < _MIN_OCR_CONFIDENCE[language]:
                    continue
                checked[language] += 1
                if _known(language, form):
                    continue
                layer = layers.get((page.page_number, language))
                if layer is not None and _lookup_key(token) not in layer:
                    continue  # the PDF itself spells it differently: an OCR misread
                unknown.setdefault((language, form), []).append((page, index, word, token))

    candidates: list[dict[str, Any]] = []
    occurrences: dict[int, list[tuple[OCRPage, int, OCRWord, str]]] = {}
    # Most frequent unknown words first, then in reading order.
    for (language, form), found in sorted(unknown.items(), key=lambda kv: -len(kv[1])):
        suggestions = _suggestions(language, form)
        if not suggestions:
            continue
        page, index, word, token = found[0]
        context = " ".join(w.text for w in page.words[max(index - _CONTEXT_WORDS, 0): index + _CONTEXT_WORDS + 1])
        candidate_id = len(candidates) + 1
        candidates.append({
            "id": candidate_id, "word": token, "language": language, "context": context, "suggestions": suggestions,
        })
        occurrences[candidate_id] = found
        if len(candidates) >= _MAX_CANDIDATES:
            break

    words_checked = sum(checked.values())
    data: dict[str, Any] = {
        "words_checked": words_checked,
        "words_checked_by_language": dict(checked),
        "unknown_words": len(unknown),
        "candidates": len(candidates),
        "text_layer_pages": sorted({p for p, _ in layers}),
    }
    if words_checked == 0:
        return {"result": "not_applicable", "details": [_scope("not_applicable", {**data, "reason": f"no printed {_languages()} text"})]}
    if not candidates:
        return {"result": "pass", "details": [_scope("pass", data)]}
    if judge is None:
        data["unconfirmed"] = [c["word"] for c in candidates]
        scope = _scope("limited", data)
        scope["description"] = (
            f"{len(candidates)} unknown word(s) close to a common word could not be confirmed as misspellings — "
            "no language model is configured, and the dictionary alone is not reliable enough to flag them."
        )
        return {"result": "limited", "details": [scope]}

    judgment = judge(document_type or "unknown", candidates)
    by_id = {c["id"]: c for c in candidates}
    findings: list[dict[str, Any]] = []
    set_aside: list[dict[str, str]] = []
    for verdict in judgment.verdicts:
        candidate = by_id.pop(verdict.id, None)
        if candidate is None:
            continue
        correction = (verdict.correction or "").strip()
        if (
            verdict.verdict != "misspelling" or verdict.confidence not in _FLAG_CONFIDENCE or not correction
            or _lookup_key(correction) == _lookup_key(candidate["word"])
        ):
            set_aside.append({"word": candidate["word"], "verdict": verdict.verdict})
            continue
        language_name = _LANGUAGE_NAMES[candidate["language"]]
        for page, _index, word, token in occurrences[candidate["id"]]:
            findings.append({
                "finding": "spelling_error",
                "severity": "low",
                "description": (
                    f"Page {page.page_number}: '{token}' looks misspelled — '{correction}' ({language_name}). "
                    f"{verdict.reasoning.strip()} Genuine documents are printed from proofread templates; a typo "
                    "can mean the text was retyped. A weak signal on its own."
                ),
                "page": page.page_number,
                "bounding_box": _box(word, page.page_number),
                "data": {
                    "text": token,
                    "correction": correction,
                    "language": candidate["language"],
                    "context": candidate["context"],
                    "model_confidence": verdict.confidence,
                    "ocr_confidence": word.confidence,
                    "confirmed_by_text_layer": (page.page_number, candidate["language"]) in layers,
                },
            })
    set_aside += [{"word": c["word"], "verdict": "no_verdict"} for c in by_id.values()]
    data["set_aside"] = set_aside
    data["misspellings"] = len(findings)
    details = findings + [_scope("flag" if findings else "pass", data)]
    return {"result": "flag" if findings else "pass", "details": details}
