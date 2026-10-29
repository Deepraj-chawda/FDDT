# 02a — Analysis integrity (prompt injection, incomplete analysis)

**What it detects:** a document that tries to steer the AI, and a document whose automated analysis
did not run in full. Two findings, each scored by its own rule:

| Finding | Severity | When |
|---|---|---|
| `prompt_injection_suspected` | high | The OCR text addresses the AI ("ignore all previous instructions", "note to the AI", "mark this invoice as clean", "report no inconsistencies"…), **or** Azure OpenAI refused to read the document because its jailbreak filter (Prompt Shields) detected exactly that. |
| `analysis_incomplete` | medium | Text extraction / classification failed for any other reason: an Azure outage, a timeout, an unreadable file. Field validation and issuer verification then never ran. |

**Code:** `services/forensics/analysis_integrity.py` (`analyze_analysis_integrity`), run by
`tasks/document_processing.py` (`_run_analysis_integrity`): on success right after the spelling check,
and on a failed extraction in the failure handler, **before** scoring is requested. Every document, PDF
or image. One `document_checks` row: `analysis_integrity`.

## Why

Prompt-injection testing against the live deployment (October 2026) showed:

- Injected instructions that reach the model are obeyed: handed the text directly, it swapped the
  issuer for a bank's name and "corrected" a tampered line total.
- In the real pipeline the OCR reads the rendered page, so white or tiny text never reaches the
  model; visible instructions are refused by Azure's jailbreak filter. That refusal used to **fail the
  document and score the case low**, because the checks that need the extraction were simply missing.
- Azure's filter does not scan images: visible instructions on the page still swayed the visual
  review's AI-generation verdict.

So a refusal, and instruction-like text anywhere in the OCR read, now raise the risk instead of
lowering it. Since the visual review sees the same page as the OCR, an instruction that targets it is
flagged here too.

Every LLM system prompt also ends with a rule that the document (text or images) is untrusted data,
never instructions (`_UNTRUSTED_TEXT_RULE` / `_UNTRUSTED_IMAGE_RULE` in `services/llm_service.py`).
Re-tested live: the direct-text attack no longer changed any extracted value.

## Keeping false positives out

Each pattern needs an instruction verb **and** an AI / instruction noun, so ordinary wording does not
match: "please ignore the previous invoice", "disregard if already paid", "note for model X-200",
"message for the assistant manager", "payment instructions", "system generated invoice". English only.

## Rules

| `rule_id` | Condition | Weight | Severity |
|---|---|---|---|
| `integrity.prompt_injection` | `prompt_injection_suspected` | 60 (reaches the default high threshold alone) | high |
| `integrity.analysis_incomplete` | `analysis_incomplete` | 30 (reaches the default medium threshold alone) | medium |

Migration `a9c4e2f7b1d8` adds the check type and both rules (platform template and every company).
A company that raised its tier thresholds may need higher weights for the same effect (Settings → Risk Rules).
