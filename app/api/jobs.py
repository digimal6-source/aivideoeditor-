"""In-process render job queue.

Jobs run on a bounded thread pool so a Codespace is never overwhelmed by
concurrent FFmpeg encodes. The manager owns nothing but bookkeeping: the actual
work lives in :class:`app.processor.render.RenderPipeline`.

The queue interface (submit / get / list / cancel) is deliberately small so it
can later be backed by Redis, Celery or a remote worker without touching the
HTTP layer.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..errors import AppError, NotFoundError
from ..models import (
    STAGE_LABELS,
    STAGE_WEIGHTS,
    JobStage,
    RenderJob,
    RenderRequest,
    RenderStatus,
)
from ..processor.render import RenderPipeline
from ..settings import Settings
from ..storage import LocalStorage, new_id

log = logging.getLogger(__name__)

#: Stages in execution order, with the cumulative weight completed before each.
_STAGE_ORDER: list[JobStage] = list(STAGE_WEIGHTS.keys())
_TOTAL_WEIGHT = sum(STAGE_WEIGHTS.values()) or 1.0
_CUMULATIVE: dict[JobStage, float] = {}
_running = 0.0
for _stage in _STAGE_ORDER:
    _CUMULATIVE[_stage] = _running
    _running += STAGE_WEIGHTS[_stage]


def compute_overall(stage: JobStage, stage_progress: float | None) -> float:
    """Map (stage, fraction-within-stage) onto a single 0..1 bar.

    Weights reflect measured relative cost, so the bar moves at a roughly even
    pace instead of sitting at 90% during the encode.
    """
    if stage in (JobStage.DONE,):
        return 1.0
    if stage not in _CUMULATIVE:
        return 0.0
    done = _CUMULATIVE[stage]
    within = STAGE_WEIGHTS[stage] * max(0.0, min(1.0, stage_progress or 0.0))
    return max(0.0, min(1.0, (done + within) / _TOTAL_WEIGHT))


class JobManager:
    def __init__(
        self, settings: Settings, storage: LocalStorage, pipeline: RenderPipeline
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.pipeline = pipeline
        self._jobs: dict[str, RenderJob] = {}
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.max_concurrent_jobs),
            thread_name_prefix="render",
        )

    # -- queries ---------------------------------------------------------

    def get(self, job_id: str) -> RenderJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError("That render job no longer exists.")
        return job

    def list(self) -> list[RenderJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def cancel(self, job_id: str) -> RenderJob:
        job = self.get(job_id)
        with self._lock:
            self._cancelled.add(job_id)
        if job.status.stage in (JobStage.DONE, JobStage.FAILED):
            return job
        job.status = RenderStatus(
            stage=JobStage.CANCELLED,
            stage_progress=None,
            overall_progress=job.status.overall_progress,
            message="Cancelled",
            determinate=False,
        )
        return job

    # -- submission ------------------------------------------------------

    def submit(self, request: RenderRequest) -> RenderJob:
        job_id = new_id("job-")
        job = RenderJob(id=job_id, request=request)
        job.status = RenderStatus(
            stage=JobStage.QUEUED,
            stage_progress=None,
            overall_progress=0.0,
            message="Queued",
            determinate=False,
        )
        with self._lock:
            self._jobs[job_id] = job
        self._pool.submit(self._run, job)
        return job

    # -- execution -------------------------------------------------------

    def _report(self, job: RenderJob):
        def report(stage: JobStage, stage_progress: float | None, message: str) -> None:
            if job.id in self._cancelled:
                raise AppError("Cancelled", code="cancelled", http_status=409)
            job.status = RenderStatus(
                stage=stage,
                stage_progress=stage_progress,
                overall_progress=compute_overall(stage, stage_progress),
                message=STAGE_LABELS.get(stage, message),
                determinate=stage_progress is not None,
            )

        return report

    def _run(self, job: RenderJob) -> None:
        job.started_at = time.time()
        try:
            outcome = self.pipeline.run(job.id, job.request, report=self._report(job))
        except AppError as exc:
            if exc.code == "cancelled":
                log.info("Job %s cancelled", job.id)
                self.storage.cleanup_job(job.id, keep_output=False)
                return
            log.warning("Job %s failed: %s", job.id, exc.message)
            self._fail(job, exc.message, exc.code)
            return
        except Exception as exc:  # pragma: no cover - unexpected only
            log.exception("Job %s crashed", job.id)
            self._fail(
                job,
                "Something went wrong while rendering. Check the server log for details.",
                "internal_error",
            )
            del exc
            return

        job.finished_at = time.time()
        job.output_path = str(outcome.output_path)
        job.output_bytes = outcome.output_path.stat().st_size
        job.output_meta = outcome.media.to_dict()
        job.caption_count = outcome.caption_count
        job.removed_silence_seconds = outcome.removed_silence_seconds
        job.warnings = list(outcome.warnings)
        job.status = RenderStatus(
            stage=JobStage.DONE,
            stage_progress=1.0,
            overall_progress=1.0,
            message=STAGE_LABELS.get(JobStage.DONE, "Done"),
            determinate=True,
        )
        log.info(
            "Job %s finished in %.1fs", job.id, (job.finished_at - (job.started_at or 0))
        )

    def _fail(self, job: RenderJob, message: str, code: str) -> None:
        job.finished_at = time.time()
        job.status = RenderStatus(
            stage=JobStage.FAILED,
            stage_progress=None,
            overall_progress=job.status.overall_progress,
            message="Failed",
            determinate=False,
            error=message,
            error_code=code,
        )
        # Failed jobs must not leave temp files behind.
        self.storage.cleanup_job(job.id, keep_output=False)

    # -- housekeeping ----------------------------------------------------

    def purge_expired(self) -> list[str]:
        removed = self.storage.purge_expired()
        cutoff = time.time() - self.settings.job_retention_hours * 3600
        with self._lock:
            stale = [
                job_id
                for job_id, job in self._jobs.items()
                if (job.finished_at or job.created_at) < cutoff
            ]
            for job_id in stale:
                self._jobs.pop(job_id, None)
        return removed + stale

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
