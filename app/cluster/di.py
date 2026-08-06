# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dependency-injection wiring for the multi-agent cluster.

Two :mod:`injector` modules, installed alongside the shared ``ModelModule``:

- :class:`ClusterModule` provides the ``ClusterConfig`` (parsed from the
  environment) and the :class:`AgentResolver` — the injectable service-discovery
  layer that turns configured peers into ``RemoteA2aAgent`` children. This is the
  "resolver so an agent can connect to the other agents in the cluster."
- :class:`SessionModule` provides the pluggable ``BaseSessionService``,
  ``BaseMemoryService``, ``BaseArtifactService``, the HITL ``ApprovalStore``,
  and the A2A ``TaskStore``
  (in-memory by default, durable backends via env — see
  ``app/cluster/session.py``, ``app/cluster/artifacts.py``,
  ``app/cluster/tasks.py``), reusing the injected GCP project/location from the
  shared module for the managed Vertex AI backends. It also provides the shared
  :class:`~app.cluster.db.Database`, so the session store and the task store
  hand out one connection pool between them rather than one each.

Build the injector once in ``app/agent.py``::

    from injector import Injector
    from app.shared.config import ModelModule, Models
    from app.cluster.di import ClusterModule, SessionModule
    from app.cluster.resolver import AgentResolver

    injector = Injector([ModelModule(), ClusterModule(), SessionModule()])
    models = injector.get(Models)
    resolver = injector.get(AgentResolver)

All provided values are real classes, so ``injector.get(...)`` type-checks
cleanly under ``ty`` (see the note in the repo AGENTS.md).
"""

from __future__ import annotations

import os

from a2a.server.tasks import TaskStore
from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.memory import BaseMemoryService
from google.adk.sessions import BaseSessionService
from injector import Module, provider, singleton

from app.agents import AGENTS, DEFAULT_AGENT
from app.cluster.approvals import ApprovalStore, build_approval_store
from app.cluster.artifacts import build_artifact_service
from app.cluster.config import AGENT_NAME_ENV, ClusterConfig
from app.cluster.db import Database, build_database
from app.cluster.resolver import AgentResolver
from app.cluster.session import build_memory_service, build_session_service
from app.cluster.tasks import build_task_store
from app.shared.project_types import GoogleCloudLocation, GoogleCloudProject


class ClusterModule(Module):
    """Provides the cluster configuration and the peer resolver."""

    @singleton
    @provider
    def provide_config(self) -> ClusterConfig:
        """Provide the (singleton) cluster configuration from the environment.

        The selected agent (``AGENT_NAME``) supplies the default peer topology:
        its ``AgentSpec.peers`` are used as the fallback when ``A2A_PEERS`` is
        unset. This keeps the DNS/URL layer here while each agent declares *who*
        it talks to in its own spec (see ``app/agents``).

        Returns:
            The resolved :class:`ClusterConfig`.
        """
        name = os.environ.get(AGENT_NAME_ENV, DEFAULT_AGENT).strip() or DEFAULT_AGENT
        spec = AGENTS.get(name)
        default_peers = spec.peers if spec is not None else ()
        return ClusterConfig.from_env(name=name, default_peers=default_peers)

    @singleton
    @provider
    def provide_resolver(self, config: ClusterConfig) -> AgentResolver:
        """Provide the (singleton) peer resolver.

        Args:
            config: The injected cluster configuration.

        Returns:
            An :class:`AgentResolver` bound to the configuration.
        """
        return AgentResolver(config)


class SessionModule(Module):
    """Provides the pluggable session, memory, artifact, and A2A task services."""

    @singleton
    @provider
    def provide_database(self) -> Database:
        """Provide the (singleton) shared database engine holder.

        ``@singleton`` is what makes the pool shared: the session service, the
        approval store and the A2A task store all take this one instance, so a
        pod opens one pool rather than three against a one-vCPU instance.

        Returns:
            The process-wide :class:`Database`.
        """
        return build_database()

    @singleton
    @provider
    def provide_session_service(
        self,
        project: GoogleCloudProject,
        location: GoogleCloudLocation,
        database: Database,
    ) -> BaseSessionService:
        """Provide the (singleton) session service selected by env.

        Args:
            project: The injected GCP project (for managed backends).
            location: The injected GCP location (for managed backends).
            database: The injected shared engine holder (for ``alloydb``).

        Returns:
            The configured :class:`BaseSessionService`.
        """
        return build_session_service(
            project=project, location=location, database=database
        )

    @singleton
    @provider
    def provide_approval_store(self, database: Database) -> ApprovalStore:
        """Provide the (singleton) HITL approval store.

        Durable on the ``hitl_approvals`` table when a database is configured,
        per-pod memory otherwise. Singleton is load-bearing rather than an
        optimisation: with the in-memory backend a second instance is a second
        dict, so approvals would split between them with no visible error.

        Args:
            database: The injected shared engine holder.

        Returns:
            The configured :class:`~app.cluster.approvals.ApprovalStore`.
        """
        return build_approval_store(database)

    @singleton
    @provider
    def provide_task_store(self, database: Database) -> TaskStore:
        """Provide the (singleton) A2A task store selected by env.

        Args:
            database: The injected shared engine holder (for ``database``).

        Returns:
            The configured :class:`~a2a.server.tasks.TaskStore`.
        """
        return build_task_store(database=database)

    @singleton
    @provider
    def provide_memory_service(
        self,
        project: GoogleCloudProject,
        location: GoogleCloudLocation,
    ) -> BaseMemoryService:
        """Provide the (singleton) memory service selected by env.

        Args:
            project: The injected GCP project (for managed backends).
            location: The injected GCP location (for managed backends).

        Returns:
            The configured :class:`BaseMemoryService`.
        """
        return build_memory_service(project=project, location=location)

    @singleton
    @provider
    def provide_artifact_service(self) -> BaseArtifactService:
        """Provide the (singleton) artifact service selected by env.

        Backed by ``cloudpathlib`` when ``ARTIFACT_STORAGE_URI`` is set, so the
        blob store is a deployment choice (``gs://``, ``s3://``, ``az://``, or a
        local path); in-memory otherwise. No project/location is injected: the
        URI carries the location and the backend authenticates with its own
        credential discovery (ADC for GCS, under Workload Identity here).

        Returns:
            The configured :class:`BaseArtifactService`.
        """
        return build_artifact_service()
