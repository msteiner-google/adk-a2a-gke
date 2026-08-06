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

"""Reusable tools available to every agent.

Mocked example logic — put tools here that all agents should expose. Anything
agent-specific stays in that agent's own `tools.py`.
"""


def echo(text: str) -> str:
    """Echo the provided text back, tagged as coming from the shared library.

    Placeholder shared tool — replace with real reusable tools (e.g. a company
    knowledge-base lookup, an internal API client, etc.).

    Args:
        text: The text to echo back.

    Returns:
        The input text, prefixed to make it obvious it came from the shared lib.
    """
    return f"[shared] {text}"
