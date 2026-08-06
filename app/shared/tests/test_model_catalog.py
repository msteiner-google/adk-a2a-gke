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

"""Tests for ModelCatalog (catalog listing pre-seeded to avoid network)."""

from google.genai import Client

from ..config import DEFAULT_LOCATION
from ..model_catalog import ModelCatalog
from ..model_selection import FLASH_FAMILY, FLASH_LITE_FAMILY, PRO_FAMILY
from .support import EMBEDDING_CATALOG, SAMPLE_CATALOG, TEST_PROJECT


def _seeded_catalog(model_ids: list[str]) -> ModelCatalog:
    """A ModelCatalog with its cached listing pre-seeded (no network call)."""
    client = Client(vertexai=True, project=TEST_PROJECT, location=DEFAULT_LOCATION)
    catalog = ModelCatalog(client)
    catalog.__dict__["_model_ids"] = model_ids
    return catalog


def test_latest_resolves_generative_families():
    catalog = _seeded_catalog(SAMPLE_CATALOG)
    assert catalog.latest(PRO_FAMILY) == "gemini-3.1-pro-preview"
    assert catalog.latest(FLASH_FAMILY) == "gemini-3.5-flash"
    assert catalog.latest(FLASH_LITE_FAMILY) == "gemini-3.1-flash-lite"


def test_latest_embedding_resolves():
    catalog = _seeded_catalog(EMBEDDING_CATALOG)
    assert catalog.latest_embedding() == "gemini-embedding-2"
