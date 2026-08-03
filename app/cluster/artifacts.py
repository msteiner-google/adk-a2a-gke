"""Pluggable artifact storage, selected by environment.

An *artifact* is a binary or textual blob an agent produces or consumes — a
generated report, a downloaded page, an image — kept out of the conversation
history and referenced by filename and version. ADK serves them through a
``BaseArtifactService``, wired here through dependency injection (see
``app/cluster/di.py``) so agent code depends only on the base class and the
concrete backend stays a deployment concern, exactly like the session service.

Backends (selected by ``ARTIFACT_STORAGE_URI``)
    - *unset* (default): ``InMemoryArtifactService`` — per-pod and ephemeral,
      which keeps unit tests and local runs hermetic (no bucket, no
      credentials).
    - set: :class:`~app.shared.artifacts.CloudPathArtifactService` bound to that
      location. ``cloudpathlib`` resolves the scheme, so the same code runs
      against GCS (``gs://bucket/prefix``), S3 (``s3://...``), Azure Blob
      (``az://...``), or a plain local directory. The cluster uses ``gs://``;
      a local path is the convenient choice when running two agents on one
      machine and wanting the artifacts to survive the process.

There is deliberately no ``ARTIFACT_BACKEND`` switch to go with
``SESSION_BACKEND`` / ``TASK_STORE_BACKEND``: the URI scheme already names the
backend, so a separate selector could only disagree with it.

**Every agent shares one artifact namespace.** Artifacts are keyed by
``{app_name}/{user_id}/{session_id}/{filename}``, and ``app_name`` is the ADK
``App`` name (``"app"``) for every agent in the cluster — not ``AGENT_NAME``. So
pointing all agents at the same ``ARTIFACT_STORAGE_URI`` is what lets a worker
save a file that the orchestrator can load back on the same session, the same
way ``shared:``-prefixed session state propagates across an A2A hop (see
``app/agents/common.py``). Give each agent its *own* prefix only if you want
that sharing to stop; the database schema-per-agent split is not mirrored here
on purpose.
"""

from __future__ import annotations

import os

from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService

from app.shared.artifacts import ARTIFACT_STORAGE_URI_ENV, CloudPathArtifactService

__all__ = ["ARTIFACT_STORAGE_URI_ENV", "build_artifact_service"]


def build_artifact_service() -> BaseArtifactService:
    """Construct the artifact service implied by ``ARTIFACT_STORAGE_URI``.

    Returns:
        A :class:`~app.shared.artifacts.CloudPathArtifactService` bound to the
        configured location, or an ``InMemoryArtifactService`` when the variable
        is unset or empty.
    """
    uri = os.environ.get(ARTIFACT_STORAGE_URI_ENV, "").strip()
    if not uri:
        return InMemoryArtifactService()
    return CloudPathArtifactService(uri)
