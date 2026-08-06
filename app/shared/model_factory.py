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

"""Factory for configured ADK `Gemini` models.

Centralizes model construction so every agent gets the same retry/backoff
policy and (optionally) the same geofenced genai client.
"""

from google.adk.models import Gemini
from google.genai import Client, types


def build_model(model: str, client: Client | None = None) -> Gemini:
    """Build a Gemini model configured with our standard retry policy.

    Centralizing this here means every caller gets the same retry/backoff
    behavior, and changing it once updates them all. Pass a `client` to
    pin the model to a specific Vertex AI project/location (see `ModelModule`).

    Args:
        model: The Gemini model name to use.
        client: An optional pre-built `google.genai.Client`. When provided it is
            patched onto the model so all calls go through that client (and its
            project/location); when omitted the model uses ADK's default client.

    Returns:
        A configured `Gemini` model instance.
    """
    model_instance = Gemini(
        model=model,
        retry_options=types.HttpRetryOptions(attempts=3),
    )
    if client is not None:
        # Route the model through the geofenced Vertex AI client.
        model_instance.api_client = client
    return model_instance
