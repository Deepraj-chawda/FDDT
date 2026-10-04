# 06 — Metadata forensics

**What it detects:** signs a PDF was edited, re-exported or assembled after it was created: editing
software fingerprints, inconsistent or missing timestamps, incremental-save revisions, XMP edit history
that contradicts the file, orphaned objects, embedded JavaScript, and a stripped metadata block.

**Code:** `services/forensics/metadata_forensics.py` (`analyze_pdf_metadata`), run by
`tasks/metadata_forensics_task.py` (`run_metadata_forensics`); stores `check_type = metadata_forensics`.

## What is read

Using **pikepdf** on the PDF's own object model (never a page render):

- the **Info dictionary** (`/Title /Author /Creator /Producer /CreationDate /ModDate`);
- the separate **XMP metadata stream**, including custom namespaces and the `xmpMM:History` revision
  chain;
- the **document ID chain** (`/ID`);
- the file's real **incremental-update / xref-revision structure** (`_walk_xref_chain`);
- **reachability**: which objects are unreachable from `/Root`/`/Info` (orphans);
- JavaScript / `OpenAction`, optional content groups, and digital-signature fields.

## Findings

Every finding is `{finding, severity, description, data?}`. `severity` is `info | low | medium | high`.
`info` findings are context (they show the raw metadata) and never affect the result.

| `finding` | Severity | Meaning |
|---|---|---|
| `pdf_unreadable` | high | The file could not be opened as a PDF |
| `metadata_entirely_stripped` | high | No Info and no XMP metadata at all |
| `editing_software_detected` | high | Producer/Creator/`softwareAgent` matches an image/graphics editor (`METADATA_FORENSICS_EDITING_SOFTWARE_NAMES`: photoshop, gimp, illustrator, affinity, coreldraw, paint.net, pixlr, canva, inkscape) |
| `mod_date_after_creation_date` | high | ModDate later than CreationDate by more than `METADATA_FORENSICS_MOD_DATE_THRESHOLD_SECONDS` (default 1 s — only jitter tolerance; being modified at all is the anomaly) |
| `info_xmp_creation_date_mismatch` | high | Info and XMP creation dates differ by > 1 minute |
| `info_xmp_mod_date_mismatch` | high | Info and XMP modification dates differ by > 1 minute |
| `info_xmp_producer_mismatch` | medium | Info `/Producer` ≠ XMP producer |
| `incremental_updates_present` | medium (1 update) / high (> 1) | The file has been saved incrementally |
| `history_editing_tool_anomaly` | medium | An XMP history entry names an editing tool |
| `history_edit_after_final_date` | high | An XMP history event is later than the file's modified date (+1 min) |
| `history_shorter_than_revisions` | medium | Fewer history entries than xref revisions |
| `history_absent_despite_revisions` | medium | Revisions exist but the history is empty |
| `orphaned_objects` | medium (orphans found) / info | Objects present but unreachable — typical of deleted content |
| `javascript_or_openaction` | medium (flagged) / info | Embedded JS or auto-run action |
| `optional_content_groups` | low / info | Layers present |
| `digital_signature` | info | Signature field present / signed / unsigned / absent |
| `info_dictionary`, `xmp_metadata`, `xmp_history`, `revision_count`, `document_id_chain` | info | Raw context |

The check `result` is **`flag` if any finding is `medium` or `high`**, else `pass`.

## Output shape (real, truncated)

```jsonc
{
  "result": "flag",
  "details": [
    {"finding": "orphaned_objects", "severity": "medium",
     "description": "Found 25 object(s) present in the file but not reachable from /Root or /Info — likely left…",
     "data": {"count": 25, "object_numbers": [2, 3, "…"]}},
    {"finding": "info_dictionary", "severity": "info", "description": "Info dictionary has 6 field(s).",
     "data": {"/Creator": "Adobe Photoshop 2024 (Windows)", "/ModDate": "D:20260902164500Z",
              "/CreationDate": "D:20260801090000Z", "/Producer": "Adobe Photoshop 2024 (Windows)", "…": "…"}}
  ]
}
```

`details` is a **list** of findings (the same shape ELA, copy-move, duplicate and visual review use, so
one findings-list UI renders them all). These findings carry no `page`/`bounding_box`; metadata is not
positional.

## Known limitations

- **PDF only.** Images and Word files are not analyzed.
- Many legitimate tools leave incremental updates or editor fingerprints (a scanned-then-annotated
  invoice, for example). A finding is a reason to look, not proof.
- A determined forger can rewrite or strip metadata; `metadata_entirely_stripped` is itself scored
  because clean-room generators rarely strip everything.
- Uploads are PDF-only, so every new document is analysed; a non-PDF stored before that rule is a quiet
  no-op.
- The visual-review task cross-references this result (`metadata_forensics_correlation`) to strengthen
  or explain its own findings; that cross-reference is deliberately **not scored** to avoid double
  counting.

## Risk rules fed

| `rule_id` | Finding | Fires on | Weight | Severity |
|---|---|---|---:|---|
| `metadata.pdf_unreadable` | `pdf_unreadable` | any | 20 | high |
| `metadata.stripped` | `metadata_entirely_stripped` | any | 12 | medium |
| `metadata.editing_software_detected` | `editing_software_detected` | any | 30 | high |
| `metadata.creation_date_mismatch` | `info_xmp_creation_date_mismatch` | any | 18 | high |
| `metadata.mod_date_mismatch` | `info_xmp_mod_date_mismatch` | any | 18 | high |
| `metadata.producer_mismatch` | `info_xmp_producer_mismatch` | any | 10 | medium |
| `metadata.modified_after_creation` | `mod_date_after_creation_date` | any | 15 | high |
| `metadata.incremental_update_single` | `incremental_updates_present` | medium | 8 | medium |
| `metadata.incremental_update_multiple` | `incremental_updates_present` | high | 20 | high |
| `metadata.history_tool_anomaly` | `history_editing_tool_anomaly` | any | 12 | medium |
| `metadata.history_edit_after_final` | `history_edit_after_final_date` | any | 22 | high |
| `metadata.history_shorter_than_revisions` | `history_shorter_than_revisions` | any | 8 | medium |
| `metadata.history_absent_despite_revisions` | `history_absent_despite_revisions` | any | 8 | medium |
| `metadata.orphaned_objects` | `orphaned_objects` | medium, high | 8 | medium |
| `metadata.javascript_or_openaction` | `javascript_or_openaction` | medium, high | 10 | medium |

Note the two severities: a rule's `severity` (how it is displayed) is independent of the *finding's*
severity (which the rule may filter on with `severity_in`).
