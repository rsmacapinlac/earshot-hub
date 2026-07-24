"""Processing jobs — durable queue in the ``jobs`` table, one in-process worker.

The queue is the table; :class:`~earshot.jobs.worker.JobWorker` is the single
thread that drains it, deciding the route (local subprocess vs. service) at
dequeue. Local transcription runs in a cancellable child process
(:mod:`earshot.jobs.transcribe`); the service route lands in M7.
"""

from earshot.jobs.serialize import job_api

__all__ = ["job_api"]
