"""Pydantic schemas shared by the pipeline and the HTTP API."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Highlight(BaseModel):
    """One candidate short, in source-video seconds."""

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class HighlightSelection(BaseModel):
    highlights: list[Highlight]
    provider: str
    raw_response: Optional[str] = None


class Artifact(BaseModel):
    name: str
    kind: Literal["video", "json", "text", "audio"]
    url: str
    title: str = ""
    start: Optional[float] = None
    end: Optional[float] = None
    size: int = 0


class Job(BaseModel):
    id: str
    status: Literal["queued", "running", "done", "error"]
    stage: str = "queued"
    progress: float = 0.0
    source: str = ""
    kind: Literal["upload", "url"] = "upload"
    error: Optional[str] = None
    provider: Optional[str] = None
    language: Optional[str] = None
    duration: Optional[float] = None
    created_at: str = ""
    log: list[str] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
