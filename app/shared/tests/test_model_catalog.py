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
