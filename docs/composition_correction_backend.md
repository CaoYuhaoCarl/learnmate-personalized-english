# Composition Correction Backend Workflow

This backend-only workflow follows the same ADK construction style as
`hitl_agent/agent.py`: Pydantic schemas, named `Agent` roles, `@node` workflow
steps, `ctx.run_node(...)` orchestration, a `RequestInput` human review pause,
and JSON-safe helpers for resumed structured outputs.

## Real Agent Integration Points

- OCR: `ocr_extraction_agent` is responsible for handwriting transcription. In
  production, run it through ADK with a multimodal model and an uploaded image
  payload. The local example uses `LocalSidecarOcrAgent` only as an offline agent
  stub so the workflow can be demonstrated without model credentials.
- Model agents: run `root_agent` through ADK with model credentials to use the
  `ocr_quality_review_agent`, `grammar_correction_agent`,
  `vocabulary_improvement_agent`, `sentence_style_improvement_agent`,
  `scoring_comment_agent`, and `final_report_synthesis_agent` definitions.
- Local sample: `run_sample_workflow()` uses deterministic local agent stubs so the
  workflow can be inspected without OCR or model credentials.

The sample image has a `.txt` sidecar file that stands in for OCR output. This
keeps the example runnable in a local environment while preserving a clear
integration point for real OCR.
