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

"""Vertex AI model-catalog access.

`ModelCatalog` wraps a `google.genai.Client`, lists the base (publisher) model
catalog once, and resolves the latest model per family using the pure selectors
in `model_selection.py`.
"""

import functools

from google.genai import Client, types

from .model_selection import select_embedding_model, select_latest_model

# Vertex AI base models are named `publishers/google/models/<id>`.
_MODEL_NAME_PREFIX = "publishers/google/models/"


class ModelCatalog:
    """Discovers the latest Gemini model per family from the Vertex AI catalog.

    The catalog is fetched once, lazily (on the first `latest` /
    `latest_embedding` call), and cached for the lifetime of the instance — the
    injector provides it as a singleton.
    """

    def __init__(self, client: Client) -> None:
        """Store the genai client used to list the model catalog.

        Args:
            client: The Vertex AI genai client to query.
        """
        self._client = client

    @functools.cached_property
    def _model_ids(self) -> list[str]:
        """List base (publisher) model ids from the catalog, once."""
        models = self._client.models.list(
            config=types.ListModelsConfig(query_base=True)
        )
        return [
            (model.name or "").removeprefix(_MODEL_NAME_PREFIX)
            for model in models
            if model.name
        ]

    def latest(self, family: str) -> str:
        """Return the latest model id in `family` (see `select_latest_model`).

        Args:
            family: One of the family keys in `model_selection`.

        Returns:
            The selected short model id.
        """
        return select_latest_model(self._model_ids, family)

    def latest_embedding(self) -> str:
        """Return the best embedding model id (see `select_embedding_model`).

        Returns:
            The selected short embedding model id.
        """
        return select_embedding_model(self._model_ids)
