"""Multi-agent cluster runtime.

This subpackage holds the **plumbing** for running a team of ADK agents on
Kubernetes and connecting them over A2A. The agents themselves live in
``app/agents`` (one folder per agent); this package is how they find and serve
each other:

- ``config``   — environment-driven cluster/agent/peer configuration (pure).
- ``resolver`` — turns peers into ``RemoteA2aAgent`` children (discovery).
- ``session``  — pluggable session/memory backends (in-memory or managed).
- ``di``       — injector modules wiring the above.
"""
