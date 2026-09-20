"""Pipeline registry for the ingest subsystem.

Defines the canonical list of ingestion pipelines. Each entry becomes a row
in the pipelines table when pipeline.db is initialized via init_db().
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineSpec:
    """Immutable descriptor for a single ingestion pipeline."""

    pipeline_id: str
    description: str
    version: str
    source_format: str
    status: str  # 'active' | 'stub'


PIPELINES: list[PipelineSpec] = [
    PipelineSpec(
        pipeline_id="ambient-transcript-ingest",
        description="Ingest completed ambient ASR sessions into meeting artifacts",
        version="0.1.0",
        source_format="ambient-jsonl",
        status="active",
    ),
    PipelineSpec(
        pipeline_id="transcript-ingest",
        description="Ingest call/meeting transcripts into structured artifacts",
        version="0.1.0",
        source_format="transcript",
        status="active",
    ),
    PipelineSpec(
        pipeline_id="customer-notes-ingest",
        description="Ingest customer-facing meeting notes into artifact store (stub)",
        version="0.1.0",
        source_format="markdown",
        status="stub",
    ),
    PipelineSpec(
        pipeline_id="pm-status-ingest",
        description="Ingest PM status reports into artifact store (stub)",
        version="0.1.0",
        source_format="markdown",
        status="stub",
    ),
]

# Convenience lookup: pipeline_id -> PipelineSpec
PIPELINE_MAP: dict[str, PipelineSpec] = {p.pipeline_id: p for p in PIPELINES}
