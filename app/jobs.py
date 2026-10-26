"""In-process job manager: background threads + thread-safe job state."""
from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from app import config
from app.models import Artifact, Highlight, Job as JobModel


class Job:
    def __init__(self, job_id: str, kind: str, source: str, workdir: Path):
        self.id = job_id
        self.kind = kind
        self.source = source
        self.workdir = workdir
        self.status = "queued"
        self.stage = "queued"
        self.progress = 0.0
        self.error: Optional[str] = None
        self.provider: Optional[str] = None
        self.language: Optional[str] = None
        self.duration: Optional[float] = None
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.log: list[str] = []
        self.highlights: list[Highlight] = []
        self.artifacts: list[dict] = []
        self._lock = threading.Lock()

    # -- mutation (always under lock) ----------------------------------------
    def update(self, *, progress: Optional[float] = None, stage: Optional[str] = None) -> None:
        with self._lock:
            if progress is not None:
                self.progress = max(0.0, min(1.0, float(progress)))
            if stage:
                self.stage = stage

    def append_log(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"[{stamp}] {line}")
            del self.log[:-200:]  # keep the tail bounded

    def finish(self, result: dict) -> None:
        with self._lock:
            self.status = "done"
            self.stage = "done"
            self.progress = 1.0
            self.provider = result.get("provider")
            self.language = result.get("language")
            self.duration = result.get("duration")
            self.highlights = [Highlight(**h) for h in result.get("highlights", [])]
            self.artifacts = list(result.get("artifacts", []))

    def fail(self, message: str) -> None:
        with self._lock:
            self.status = "error"
            self.stage = "error"
            self.error = message

    # -- snapshot ------------------------------------------------------------
    def to_model(self) -> JobModel:
        with self._lock:
            artifacts: list[Artifact] = []
            for item in self.artifacts:
                name = item.get("name", "")
                path = self.workdir / name
                artifacts.append(
                    Artifact(
                        name=name,
                        kind=item.get("kind", "json"),
                        url=f"/api/jobs/{self.id}/files/{name}",
                        title=item.get("title", ""),
                        start=item.get("start"),
                        end=item.get("end"),
                        size=path.stat().st_size if path.exists() else 0,
                    )
                )
            return JobModel(
                id=self.id,
                status=self.status,  # type: ignore[arg-type]
                stage=self.stage,
                progress=round(self.progress, 4),
                source=self.source,
                kind=self.kind,  # type: ignore[arg-type]
                error=self.error,
                provider=self.provider,
                language=self.language,
                duration=self.duration,
                created_at=self.created_at,
                log=list(self.log),
                highlights=list(self.highlights),
                artifacts=artifacts,
            )


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, source: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        workdir = config.JOBS_DIR / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        job = Job(job_id, kind, source, workdir)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def start(self, job: Job, fn: Callable[[Job], dict]) -> None:
        def _worker() -> None:
            job.status = "running"
            try:
                result = fn(job)
                job.finish(result)
            except Exception as exc:  # noqa: BLE001 - surface any failure to the client
                traceback.print_exc()
                job.append_log(f"FAILED: {exc}")
                job.fail(str(exc))

        thread = threading.Thread(target=_worker, name=f"job-{job.id}", daemon=True)
        thread.start()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
