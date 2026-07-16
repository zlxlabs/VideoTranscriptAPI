"""Single normalization contract for all per-request LLM feature gates."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ProcessingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calibrate: StrictBool = Field(True, description="Run LLM calibration")
    summarize: StrictBool = Field(True, description="Generate summary and related metadata")
    infer_speaker_names: StrictBool = Field(True, description="Infer real speaker names")


DEFAULT_PROCESSING_OPTIONS = ProcessingOptions().model_dump()


def normalize_processing_options(value: Any = None) -> dict[str, bool]:
    """Return a fresh, complete, strictly validated feature-gate mapping."""
    if value is None:
        return dict(DEFAULT_PROCESSING_OPTIONS)
    if isinstance(value, ProcessingOptions):
        return value.model_dump()
    return ProcessingOptions.model_validate(value).model_dump()
