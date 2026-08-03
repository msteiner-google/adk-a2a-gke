"""Tests for the shared ``model_factory.build_model`` helper."""

from google.genai import Client

from ..config import DEFAULT_LOCATION
from ..model_factory import build_model
from .support import TEST_PROJECT


def test_build_model_accepts_model_name():
    model = build_model("gemini-2.5-pro")
    assert model.model == "gemini-2.5-pro"


def test_build_model_sets_retry_policy():
    model = build_model("gemini-2.5-flash")
    assert model.retry_options is not None
    assert model.retry_options.attempts == 3


def test_build_model_patches_client():
    client = Client(vertexai=True, project=TEST_PROJECT, location=DEFAULT_LOCATION)
    model = build_model("gemini-2.5-flash", client=client)
    assert model.api_client is client
