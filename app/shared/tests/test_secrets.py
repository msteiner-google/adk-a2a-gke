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

"""Tests for the Secret Manager wiring (hermetic; fake client, no network)."""

from typing import NewType, cast

import pytest
from google.cloud.secretmanager import SecretManagerServiceClient
from injector import Injector, provider, singleton

from ..config import ModelModule
from ..project_types import GoogleCloudProject
from ..secrets import SecretModule, SecretResolver, Secrets
from .support import TEST_PROJECT, FakeSecretClient

ApiKey = NewType("ApiKey", str)

_SHORT_NAME = "api-key"
_RESOURCE = f"projects/{TEST_PROJECT}/secrets/{_SHORT_NAME}/versions/latest"
_FULL_NAME = "projects/other/secrets/db-password/versions/3"


def _secrets(
    values: dict[str, str], project: str = TEST_PROJECT
) -> tuple[Secrets, FakeSecretClient]:
    """Build a `Secrets` over a fake client, returning both for assertions."""
    fake = FakeSecretClient(values)
    secrets = Secrets(
        cast(SecretManagerServiceClient, fake), GoogleCloudProject(project)
    )
    return secrets, fake


# --- resource-name resolution -----------------------------------------------


def test_short_name_expands_against_project():
    secrets, _ = _secrets({})
    assert secrets.resource_name(_SHORT_NAME) == _RESOURCE


def test_short_name_honors_version():
    secrets, _ = _secrets({})
    assert secrets.resource_name(_SHORT_NAME, "7").endswith("/versions/7")


def test_fully_qualified_name_passes_through():
    secrets, _ = _secrets({})
    assert secrets.resource_name(_FULL_NAME) == _FULL_NAME


def test_short_name_without_project_raises():
    secrets, _ = _secrets({}, project="")
    with pytest.raises(ValueError, match="without a project"):
        secrets.resource_name(_SHORT_NAME)


# --- get / decoding / caching -----------------------------------------------


def test_get_fetches_and_decodes():
    secrets, fake = _secrets({_RESOURCE: "s3cr3t"})
    assert secrets.get(_SHORT_NAME) == "s3cr3t"
    assert fake.requested == [_RESOURCE]


def test_get_accepts_full_resource_name():
    secrets, fake = _secrets({_FULL_NAME: "pw"})
    assert secrets.get(_FULL_NAME) == "pw"
    assert fake.requested == [_FULL_NAME]


def test_get_caches_per_resource():
    secrets, fake = _secrets({_RESOURCE: "v1"})
    assert secrets.get(_SHORT_NAME) == "v1"
    assert secrets.get(_SHORT_NAME) == "v1"
    assert fake.requested == [_RESOURCE]  # fetched once


def test_refresh_bypasses_cache():
    secrets, fake = _secrets({_RESOURCE: "v1"})
    assert secrets.get(_SHORT_NAME) == "v1"
    fake.values[_RESOURCE] = "v2"
    assert secrets.get(_SHORT_NAME, refresh=True) == "v2"
    assert fake.requested == [_RESOURCE, _RESOURCE]


def test_distinct_versions_cached_separately():
    v1 = f"projects/{TEST_PROJECT}/secrets/{_SHORT_NAME}/versions/1"
    secrets, fake = _secrets({_RESOURCE: "latest-val", v1: "v1-val"})
    assert secrets.get(_SHORT_NAME) == "latest-val"
    assert secrets.get(_SHORT_NAME, "1") == "v1-val"
    assert fake.requested == [_RESOURCE, v1]


# --- typed resolution -------------------------------------------------------


def test_get_as_wraps_value():
    secrets, _ = _secrets({_RESOURCE: "abc"})
    assert secrets.get_as(ApiKey, _SHORT_NAME) == ApiKey("abc")


def test_resolver_returns_typed_value():
    secrets, _ = _secrets({_RESOURCE: "abc"})
    resolve = SecretResolver(secrets)
    assert resolve(ApiKey, _SHORT_NAME) == "abc"


# --- module wiring ----------------------------------------------------------


class _FakeSecretModule(SecretModule):
    """SecretModule whose client is a pre-built fake (no creds / network)."""

    def __init__(self, client: FakeSecretClient) -> None:
        self._client = client

    @singleton
    @provider
    def provide_client(self) -> SecretManagerServiceClient:
        return cast(SecretManagerServiceClient, self._client)


def _injector(values: dict[str, str]) -> Injector:
    # ModelModule binds GoogleCloudProject; it never builds a genai client here
    # because nothing resolved below depends on one.
    return Injector(
        [ModelModule(project=TEST_PROJECT), _FakeSecretModule(FakeSecretClient(values))]
    )


def test_module_provides_working_secrets():
    secrets = _injector({_RESOURCE: "wired"}).get(Secrets)
    assert secrets.get(_SHORT_NAME) == "wired"


def test_module_provides_singletons():
    injector = _injector({_RESOURCE: "wired"})
    assert injector.get(Secrets) is injector.get(Secrets)
    assert injector.get(SecretResolver) is injector.get(SecretResolver)


def test_module_resolver_reads_secret():
    resolve = _injector({_RESOURCE: "wired"}).get(SecretResolver)
    assert resolve(ApiKey, _SHORT_NAME) == "wired"
