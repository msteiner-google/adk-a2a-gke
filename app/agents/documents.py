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

"""Reading documents passed by reference (the claim-check pattern, D4).

A caller hands a specialist a *pointer* — ``gs://bucket/cases/123/dossier.pdf`` —
not a document. The specialist reads it here, with its own credentials and its
own domain knowledge of what matters in it.

Why not just put the document in the payload
--------------------------------------------
Two reasons, and the second is the interesting one. A multi-megabyte filing
blows the A2A payload and the model's context. But the real problem is *who does
the extracting*: if the caller has to summarise the document down to something
that fits, the caller is performing domain work it is not qualified for. A
planner deciding which clauses of a corporate registration matter is guessing;
the entity specialist knows. Passing the reference keeps that judgement where
the expertise is.

Why not the shared artifact service
-----------------------------------
ADK's artifact service keys blobs by app name, user and session, so reaching one
means sharing a session — the implicit coupling this architecture removes. A URI
in the payload is explicit, framework-neutral (a LangGraph specialist reads the
same pointer with its own SDK) and independently authorizable: access is granted
per prefix, so a specialist owned by another team can be given exactly the
documents for one case and nothing else.

Access control
--------------
Reads use the pod's own credentials — under Workload Identity, its Kubernetes
service account's GSA. The bucket layout is case-prefixed
(``<bucket>/cases/<case_id>/…``) precisely so an IAM condition can scope a
specialist to a prefix. A specialist hosted outside this cluster should instead
be handed a short-lived signed URL; the ``https://`` scheme below works
unchanged for that, which is the point of accepting a URI rather than a bucket
and key.
"""

from __future__ import annotations

from typing import Any

# Read at most this much of any single document into the model's context. A
# specialist that needs more should chunk or summarise deliberately rather than
# discovering the limit as a truncated answer.
MAX_CHARS = 20_000

# Schemes a document reference may use. A bare local path is accepted too (it is
# what the tests and local runs use), but anything else is refused rather than
# quietly resolved -- a typo'd scheme must not become a local filesystem read.
ALLOWED_SCHEMES = ("gs://", "s3://", "az://", "https://", "file://")


def _looks_remote(ref: str) -> bool:
    """Whether a reference names a remote object store.

    Args:
        ref: The document reference.

    Returns:
        ``True`` when the reference carries a known remote scheme.
    """
    return ref.startswith(("gs://", "s3://", "az://", "https://"))


def read_document(reference: str) -> dict[str, Any]:
    """Read a document the caller passed by reference.

    Args:
        reference: An object-store URI such as
            ``gs://bucket/cases/123/dossier.pdf``, or a local path.

    Returns:
        A mapping with the document's text, or an ``error`` status explaining
        why it could not be read. Errors are returned rather than raised so the
        model can report the problem instead of the turn failing.
    """
    if not reference or not reference.strip():
        return {"status": "error", "reference": reference, "error": "empty reference"}
    ref = reference.strip()

    if "://" in ref and not ref.startswith(ALLOWED_SCHEMES):
        return {
            "status": "error",
            "reference": ref,
            "error": (
                f"unsupported scheme; expected one of {', '.join(ALLOWED_SCHEMES)}"
            ),
        }

    try:
        # cloudpathlib returns a plain pathlib.Path for a non-URI, so one code
        # path covers gs://, s3://, az:// and a local file. Imported lazily:
        # this pulls in cloud SDKs, and an agent with no documents to read
        # should not pay for them at import.
        from cloudpathlib import AnyPath

        path = AnyPath(ref)
        if not path.exists():
            return {"status": "error", "reference": ref, "error": "not found"}
        body = path.read_text(errors="replace")
    except Exception as exc:
        # Credentials, network, a binary file that is not text -- all of them
        # are the specialist's problem to report, not the caller's to guess at.
        return {"status": "error", "reference": ref, "error": str(exc)}

    truncated = len(body) > MAX_CHARS
    return {
        "status": "ok",
        "reference": ref,
        "remote": _looks_remote(ref),
        "truncated": truncated,
        "chars": len(body),
        "text": body[:MAX_CHARS],
    }
