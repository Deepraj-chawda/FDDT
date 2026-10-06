"""
AI-generated-document detection — NOT a standalone service/check_type.

SPECIFICATION.md sections 2/3.4 name "Azure AI Content Safety or Hive
Moderation — pick one" for this. Investigated 2026-09-17 and neither
panned out as a standalone integration: Azure AI Content Safety has no
synthetic-content/AI-generated-image detection capability at all
(confirmed against Microsoft's own "what's new" changelog and REST API
reference — its only image endpoint scores four harm categories: Hate,
SelfHarm, Sexual, Violence, nothing related to AI-generation), and Hive
Moderation has no workable free tier for a local dev foundation.

The AI-generation question is instead asked as a sixth structured item
inside app/services/visual_inconsistency_service.py's per-page Azure
OpenAI vision call (check_type "visual_inconsistency_review") — see
that module's docstring. This file is kept only as the documented
record of that decision; there is no ai_content_detection-specific
service code. The `ai_content_detection` DocumentCheckType enum value
(app/models/document_check.py) is likewise left in place but unused,
same as other superseded-but-harmless enum values in this codebase
(e.g. `issuer_validation`).
"""
