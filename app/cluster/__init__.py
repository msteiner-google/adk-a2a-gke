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

"""Multi-agent cluster runtime.

This subpackage holds the **plumbing** for running a team of ADK agents on
Kubernetes and connecting them over A2A. The agents themselves live in
``app/agents`` (one folder per agent); this package is how they find and serve
each other:

- ``config``   — environment-driven cluster/agent/peer configuration (pure).
- ``resolver`` — turns peers into ``ResumingA2aAgent`` children (discovery).
- ``resume``   — keeps a human's decision a *function response* so the peer
  that is paused on it can actually resume (google/adk-python#6721).
- ``session``  — pluggable session/memory backends (in-memory or managed).
- ``di``       — injector modules wiring the above.
"""
