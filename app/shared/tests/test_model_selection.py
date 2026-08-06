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

"""Tests for the pure model-id parsing / selection logic (no network)."""

import pytest

from ..model_selection import (
    FLASH_FAMILY,
    FLASH_LITE_FAMILY,
    PRO_FAMILY,
    _classify_embedding_model,
    _classify_model,
    select_embedding_model,
    select_latest_model,
)
from .support import EMBEDDING_CATALOG, SAMPLE_CATALOG

# --- generative parsing / selection ----------------------------------------


def test_classify_model_keeps_text_families():
    pro = _classify_model("gemini-2.5-pro")
    flash = _classify_model("gemini-2.5-flash")
    flash_lite = _classify_model("gemini-2.5-flash-lite")
    assert pro is not None and pro[1] == PRO_FAMILY
    assert flash is not None and flash[1] == FLASH_FAMILY
    assert flash_lite is not None and flash_lite[1] == FLASH_LITE_FAMILY


@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-2.5-flash-tts",
        "gemini-2.5-pro-tts",
        "gemini-3.1-flash-image",
        "gemini-3-pro-image",
        "gemini-embedding-2",
        "gemini-live-2.5-flash-native-audio",
        "text-bison",
    ],
)
def test_classify_model_skips_specialized(model_id):
    assert _classify_model(model_id) is None


def test_select_latest_picks_highest_version_including_preview():
    assert select_latest_model(SAMPLE_CATALOG, PRO_FAMILY) == "gemini-3.1-pro-preview"
    assert select_latest_model(SAMPLE_CATALOG, FLASH_FAMILY) == "gemini-3.5-flash"
    assert (
        select_latest_model(SAMPLE_CATALOG, FLASH_LITE_FAMILY)
        == "gemini-3.1-flash-lite"
    )


def test_select_latest_prefers_stable_and_plain_alias_on_ties():
    ids = [
        "gemini-2.5-flash-preview-04-17",
        "gemini-2.5-flash-001",
        "gemini-2.5-flash",
    ]
    assert select_latest_model(ids, FLASH_FAMILY) == "gemini-2.5-flash"


def test_select_latest_prefers_higher_numeric_suffix():
    ids = ["gemini-2.0-flash-001", "gemini-2.0-flash-002"]
    assert select_latest_model(ids, FLASH_FAMILY) == "gemini-2.0-flash-002"


def test_select_latest_prefers_newer_preview_date():
    ids = ["gemini-2.5-pro-preview-04-17", "gemini-2.5-pro-preview-05-20"]
    assert select_latest_model(ids, PRO_FAMILY) == "gemini-2.5-pro-preview-05-20"


def test_select_latest_raises_when_family_absent():
    with pytest.raises(ValueError, match="No Gemini model found for family 'pro'"):
        select_latest_model(["gemini-2.5-flash", "gemini-embedding-2"], PRO_FAMILY)


# --- embedding parsing / selection -----------------------------------------


@pytest.mark.parametrize(
    "model_id",
    ["multimodalembedding", "textembedding-gecko", "gemini-2.5-flash"],
)
def test_classify_embedding_skips_unsupported(model_id):
    assert _classify_embedding_model(model_id) is None


def test_select_embedding_prefers_gemini_family():
    # gemini-embedding wins over text-embedding regardless of version, and the
    # latest gemini-embedding version is chosen.
    assert select_embedding_model(EMBEDDING_CATALOG) == "gemini-embedding-2"


def test_select_embedding_family_precedence():
    # gemini-embedding beats a higher-numbered text-embedding.
    ids = ["text-embedding-999", "gemini-embedding-001"]
    assert select_embedding_model(ids) == "gemini-embedding-001"


def test_select_embedding_falls_through_family_order():
    assert select_embedding_model(["text-embedding-005"]) == "text-embedding-005"
    assert (
        select_embedding_model(
            ["text-multilingual-embedding-002", "multimodalembedding"]
        )
        == "text-multilingual-embedding-002"
    )
    assert select_embedding_model(["embeddinggemma", "textembedding-gecko"]) == (
        "embeddinggemma"
    )


def test_select_embedding_prefers_stable_over_variant():
    ids = ["text-embedding-large-exp-03-07", "text-embedding-005"]
    assert select_embedding_model(ids) == "text-embedding-005"


def test_select_embedding_raises_when_none_found():
    with pytest.raises(ValueError, match="No supported embedding model"):
        select_embedding_model(["multimodalembedding", "textembedding-gecko"])
