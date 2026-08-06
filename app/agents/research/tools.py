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

"""Tools specific to the research agent."""

from __future__ import annotations


def web_search(query: str) -> dict[str, str]:
    """Look up information for a query (deterministic placeholder).

    This is a stub so the research agent works out of the box and in hermetic
    tests. Replace it with a real search/RAG integration for your use case.

    Args:
        query: The search query.

    Returns:
        A mapping echoing the query with a placeholder result.
    """
    return {
        "status": "ok",
        "query": query,
        "result": f"No live search configured; replace web_search to answer: {query}",
    }
