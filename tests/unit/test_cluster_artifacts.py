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

"""Unit tests for the artifact-storage backend selection.

Hermetic: the `cloudpathlib` service is exercised against a local directory
(`AnyPath` yields a plain `pathlib.Path` for a non-URI path), so no bucket and
no credentials are involved.
"""

import pytest
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.genai import types

from app.cluster.artifacts import ARTIFACT_STORAGE_URI_ENV, build_artifact_service
from app.shared.artifacts import CloudPathArtifactService


def test_defaults_to_in_memory(monkeypatch):
    monkeypatch.delenv(ARTIFACT_STORAGE_URI_ENV, raising=False)
    assert isinstance(build_artifact_service(), InMemoryArtifactService)


def test_blank_uri_is_in_memory(monkeypatch):
    monkeypatch.setenv(ARTIFACT_STORAGE_URI_ENV, "   ")
    assert isinstance(build_artifact_service(), InMemoryArtifactService)


def test_uri_selects_cloudpath_service(monkeypatch, tmp_path):
    monkeypatch.setenv(ARTIFACT_STORAGE_URI_ENV, str(tmp_path))
    assert isinstance(build_artifact_service(), CloudPathArtifactService)


@pytest.mark.asyncio
async def test_cloudpath_service_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv(ARTIFACT_STORAGE_URI_ENV, str(tmp_path))
    service = build_artifact_service()

    version = await service.save_artifact(
        app_name="app",
        user_id="u1",
        session_id="s1",
        filename="report.txt",
        artifact=types.Part(text="hello"),
    )
    assert version == 0

    loaded = await service.load_artifact(
        app_name="app", user_id="u1", session_id="s1", filename="report.txt"
    )
    assert loaded is not None
    assert loaded.text == "hello"
    # Written under the configured base path, not somewhere else.
    assert (tmp_path / "app" / "u1" / "s1" / "report.txt" / "0").is_file()


@pytest.mark.asyncio
async def test_injector_provides_the_configured_service(monkeypatch, tmp_path):
    """The DI wiring, not just the builder, honours the storage URI."""
    from google.adk.artifacts.base_artifact_service import BaseArtifactService
    from injector import Injector

    from app.cluster.di import SessionModule

    monkeypatch.setenv(ARTIFACT_STORAGE_URI_ENV, str(tmp_path))
    injector = Injector([SessionModule()])
    service = injector.get(BaseArtifactService)

    assert isinstance(service, CloudPathArtifactService)
    # Singleton: the serving layer and the Runner must share one instance.
    assert injector.get(BaseArtifactService) is service
