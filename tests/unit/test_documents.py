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

"""Unit tests for reading documents passed by reference (D4, claim-check).

Hermetic: cloudpathlib hands back a plain ``pathlib.Path`` for a non-URI, so the
same code path that reads ``gs://`` is exercised against ``tmp_path`` with no
bucket and no credentials — the same trick ``test_cluster_artifacts.py`` uses.
"""

from app.agents.contracts import PAYLOADS, ResearchRequest
from app.agents.documents import MAX_CHARS, read_document


def test_reads_a_local_document(tmp_path):
    doc = tmp_path / "dossier.txt"
    doc.write_text("BNP Paribas, registered in IE, company number 12345.")
    result = read_document(str(doc))
    assert result["status"] == "ok"
    assert "company number 12345" in result["text"]
    assert result["remote"] is False
    assert result["truncated"] is False


def test_missing_document_is_reported_not_raised(tmp_path):
    # The model has to be able to say "I could not read it"; an exception would
    # fail the whole turn and tell the caller nothing useful.
    result = read_document(str(tmp_path / "absent.txt"))
    assert result["status"] == "error"
    assert result["error"] == "not found"


def test_empty_reference_is_rejected():
    assert read_document("   ")["status"] == "error"


def test_unknown_scheme_is_refused_not_resolved():
    # A typo'd scheme must not silently fall through to a local filesystem read.
    result = read_document("gcs://bucket/oops.pdf")
    assert result["status"] == "error"
    assert "unsupported scheme" in result["error"]


def test_long_documents_are_truncated_and_say_so(tmp_path):
    doc = tmp_path / "big.txt"
    doc.write_text("x" * (MAX_CHARS + 500))
    result = read_document(str(doc))
    assert result["truncated"] is True
    assert len(result["text"]) == MAX_CHARS
    # The untruncated size is reported, so a specialist can decide to chunk
    # rather than quietly answering from a fraction of the document.
    assert result["chars"] == MAX_CHARS + 500


def test_every_contract_can_carry_document_references():
    # The claim-check field belongs to the envelope, not to one specialist: any
    # agent may be handed a document.
    for schema in PAYLOADS.values():
        assert "document_refs" in schema.model_fields


def test_document_refs_default_to_empty():
    assert ResearchRequest(case_id="c1", question="q").document_refs == []


def test_document_refs_are_declared_in_the_tool_schema():
    schema = ResearchRequest.model_json_schema()
    prop = schema["properties"]["document_refs"]
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "string"
    # The description is what tells the calling model to pass a pointer rather
    # than pasting the document in.
    assert "reference" in prop["description"]
