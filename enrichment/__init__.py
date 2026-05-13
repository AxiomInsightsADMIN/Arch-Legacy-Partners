"""Phase 4 enrichment package.

Per-canonical acceptance-flag enrichment via Brave Search + Anthropic
Haiku 4.5 structured extraction. Cached by content hash in
`llm_enrichment_cache` for idempotency.

Public surface lives in `enrichment.enrich`; sub-modules are internal.
"""
