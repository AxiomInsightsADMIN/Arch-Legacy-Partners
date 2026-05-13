"""Phase 4.5 discovery package.

Bounded query-driven discovery crawl targeting gap categories from
docs/v1_scope_limitations.md. Templates live in config/discovery_queries.yaml;
the harvester executes them through Brave Search (paid tier) and populates
the discovered_url table for downstream Haiku extraction.

Sub-modules:
  - discovery_crawl: URL harvester (Phase 4.5 step B)
  - (TBD) discovery_extract: per-URL fetch + Haiku extraction (step C)
  - (TBD) discovery_resolve: resolver integration + review queue (step D)
"""
