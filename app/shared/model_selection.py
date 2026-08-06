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

"""Pure model-id parsing and "latest model" selection logic.

This module has **no** genai / injector / ADK dependencies — it only reasons
about catalog model-id *strings* (e.g. ``gemini-2.5-flash-lite``), so it is
trivially unit-testable in isolation. `ModelCatalog` (in `model_catalog.py`)
feeds the live catalog ids into the `select_*` functions here.
"""

import re
from collections.abc import Iterable

# Gemini generative model-family keys.
FLASH_LITE_FAMILY = "flash-lite"
FLASH_FAMILY = "flash"
PRO_FAMILY = "pro"

# Embedding model families in PRIORITY order (highest first). The best model is
# the latest version within the highest-priority family that has any match;
# `gemini-embedding` always wins over `text-embedding`, etc. Models that match
# none of these (e.g. `multimodalembedding`, `textembedding-gecko`) are ignored.
EMBEDDING_FAMILIES = (
    "gemini-embedding",
    "text-embedding",
    "text-multilingual-embedding",
    "embeddinggemma",
)

# A base id looks like `gemini-<version>-<rest>`, e.g. `gemini-2.5-flash` or
# `gemini-2.0-flash-lite-001`. Capture the version and the remainder.
_MODEL_ID_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-(.+)$")

# Trailing markers that denote a preview / experimental release.
_PREVIEW_MARKERS = frozenset({"preview", "exp"})


def _classify_model(
    model_id: str,
) -> tuple[tuple[int, ...], str, bool, bool, tuple[int, ...]] | None:
    """Parse a Gemini model id into sortable metadata, or None to skip it.

    Only general-purpose text models in the `pro`, `flash`, and `flash-lite`
    families are kept; specialized variants (``-tts``, ``-image``, ``-embedding``,
    ``-live-...``, etc.) and non-Gemini models return None.

    Args:
        model_id: The short model id, e.g. ``gemini-2.5-flash-lite``.

    Returns:
        A tuple of ``(version, family, is_preview, is_plain, suffix_numbers)``,
        or None if the id is not a supported text model.
    """
    match = _MODEL_ID_RE.match(model_id)
    if match is None:
        return None
    version = tuple(int(part) for part in match.group(1).split("."))
    tokens = match.group(2).split("-")
    if tokens[:2] == ["flash", "lite"]:
        family, extra = FLASH_LITE_FAMILY, tokens[2:]
    elif tokens[0] == "flash":
        family, extra = FLASH_FAMILY, tokens[1:]
    elif tokens[0] == "pro":
        family, extra = PRO_FAMILY, tokens[1:]
    else:
        return None
    # After the family, only a numeric version suffix (e.g. `-001`) or a
    # preview/exp marker is allowed; anything else is a specialized variant.
    if extra and not (extra[0].isdigit() or extra[0] in _PREVIEW_MARKERS):
        return None
    is_preview = bool(extra) and extra[0] in _PREVIEW_MARKERS
    is_plain = not extra
    suffix_numbers = tuple(int(token) for token in extra if token.isdigit())
    return version, family, is_preview, is_plain, suffix_numbers


def select_latest_model(model_ids: Iterable[str], family: str) -> str:
    """Pick the latest model id in a family from a list of catalog ids.

    "Latest" means the highest version, including previews. Ties break toward a
    stable (non-preview) release, then the unversioned family alias (e.g.
    ``gemini-2.5-flash`` over ``gemini-2.5-flash-001``), then the highest numeric
    suffix / preview date.

    Args:
        model_ids: Candidate short model ids from the catalog.
        family: One of `FLASH_LITE_FAMILY`, `FLASH_FAMILY`, `PRO_FAMILY`.

    Returns:
        The selected short model id.

    Raises:
        ValueError: If no model in the family is found.
    """
    best_id: str | None = None
    best_key: tuple | None = None
    for model_id in model_ids:
        parsed = _classify_model(model_id)
        if parsed is None:
            continue
        version, model_family, is_preview, is_plain, suffix_numbers = parsed
        if model_family != family:
            continue
        key = (version, 0 if is_preview else 1, 1 if is_plain else 0, suffix_numbers)
        if best_key is None or key > best_key:
            best_key, best_id = key, model_id
    if best_id is None:
        raise ValueError(
            f"No Gemini model found for family '{family}' in the Vertex AI "
            "catalog. Set the corresponding GEMINI_*_MODEL env var to pin one."
        )
    return best_id


def _classify_embedding_model(
    model_id: str,
) -> tuple[int, bool, tuple[int, ...]] | None:
    """Rank an embedding model id, or None if it is not a supported family.

    Args:
        model_id: The short model id, e.g. ``gemini-embedding-001``.

    Returns:
        A sortable tuple ``(family_priority, is_stable, suffix_numbers)`` where a
        higher `family_priority` means a more-preferred family (see
        `EMBEDDING_FAMILIES`), or None for models outside those families.
    """
    for rank, family in enumerate(EMBEDDING_FAMILIES):
        if model_id != family and not model_id.startswith(f"{family}-"):
            continue
        remainder = model_id[len(family) :].lstrip("-")
        tokens = remainder.split("-") if remainder else []
        # Stable = a plain family alias or a numeric-versioned release; a
        # non-numeric first token marks a variant (e.g. `-large-exp-...`).
        is_stable = not tokens or tokens[0].isdigit()
        suffix_numbers = tuple(int(token) for token in tokens if token.isdigit())
        family_priority = len(EMBEDDING_FAMILIES) - rank
        return family_priority, is_stable, suffix_numbers
    return None


def select_embedding_model(model_ids: Iterable[str]) -> str:
    """Pick the best embedding model id from a list of catalog ids.

    Selection is by family priority first (`gemini-embedding` beats
    `text-embedding`, etc. — see `EMBEDDING_FAMILIES`), then, within the winning
    family, a stable release over a variant, then the highest version.

    Args:
        model_ids: Candidate short model ids from the catalog.

    Returns:
        The selected short embedding model id.

    Raises:
        ValueError: If no supported embedding model is found.
    """
    best_id: str | None = None
    best_key: tuple[int, bool, tuple[int, ...]] | None = None
    for model_id in model_ids:
        key = _classify_embedding_model(model_id)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key, best_id = key, model_id
    if best_id is None:
        raise ValueError(
            "No supported embedding model found in the Vertex AI catalog. Set "
            "GEMINI_EMBEDDING_MODEL to pin one."
        )
    return best_id
