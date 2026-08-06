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
