"""Processing jobs — durable queue in the ``jobs`` table, one in-process worker.

The worker, routing (local subprocess vs. service), retry semantics, and
preemption land in the jobs and service milestones. This package currently
provides the job serialiser used by the API and store.
"""

from earshot.jobs.serialize import job_api

__all__ = ["job_api"]
