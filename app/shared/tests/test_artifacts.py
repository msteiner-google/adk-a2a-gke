"""Tests for the cloud-agnostic artifact service (hermetic; local backend).

`CloudPathArtifactService` resolves its base path with `cloudpathlib.AnyPath`,
which returns a plain `pathlib.Path` for a local directory — so pointing it at a
`tmp_path` exercises the exact same code path used against `s3://` / `gs://`
without any network or credentials.
"""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types
from injector import Injector

from ..artifacts import (
    ARTIFACT_STORAGE_URI_ENV,
    ArtifactStorageModule,
    CloudPathArtifactService,
)

_APP = "app"
_USER = "u1"
_SESSION = "s1"


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def _service(tmp_path: Path) -> CloudPathArtifactService:
    """Build a service rooted at a local temp dir (backend-agnostic path)."""
    return CloudPathArtifactService(tmp_path / "artifacts")


def _text(value: str) -> types.Part:
    return types.Part(text=value)


def _blob(data: bytes, mime: str = "image/png", name: str | None = None) -> types.Part:
    return types.Part(
        inline_data=types.Blob(mime_type=mime, data=data, display_name=name)
    )


# --- save / load round-trips ------------------------------------------------


def test_save_returns_incrementing_versions(tmp_path: Path):
    svc = _service(tmp_path)
    v0 = _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            artifact=_text("one"),
        )
    )
    v1 = _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            artifact=_text("two"),
        )
    )
    assert (v0, v1) == (0, 1)


def test_load_latest_by_default(tmp_path: Path):
    svc = _service(tmp_path)
    for value in ("one", "two", "three"):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id=_USER,
                session_id=_SESSION,
                filename="a.txt",
                artifact=_text(value),
            )
        )
    latest = _run(
        svc.load_artifact(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
        )
    )
    assert latest is not None
    assert latest.text == "three"


def test_load_specific_version(tmp_path: Path):
    svc = _service(tmp_path)
    for value in ("one", "two"):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id=_USER,
                session_id=_SESSION,
                filename="a.txt",
                artifact=_text(value),
            )
        )
    part = _run(
        svc.load_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            version=0,
        )
    )
    assert part is not None
    assert part.text == "one"


def test_binary_round_trip_preserves_bytes_and_mime(tmp_path: Path):
    svc = _service(tmp_path)
    payload = bytes(range(256))
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="img.png",
            artifact=_blob(payload, mime="image/png"),
        )
    )
    part = _run(
        svc.load_artifact(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="img.png"
        )
    )
    assert part is not None
    assert part.inline_data is not None
    assert part.inline_data.data == payload
    assert part.inline_data.mime_type == "image/png"


def test_display_name_preserved(tmp_path: Path):
    svc = _service(tmp_path)
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="img.png",
            artifact=_blob(b"data", name="Nice Picture"),
        )
    )
    part = _run(
        svc.load_artifact(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="img.png"
        )
    )
    assert part is not None
    assert part.inline_data is not None
    assert part.inline_data.display_name == "Nice Picture"


def test_load_missing_returns_none(tmp_path: Path):
    svc = _service(tmp_path)
    part = _run(
        svc.load_artifact(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="nope.txt"
        )
    )
    assert part is None


# --- dict artifacts + custom metadata ---------------------------------------


def test_accepts_camelcase_dict_artifact(tmp_path: Path):
    svc = _service(tmp_path)
    # External callers may pass a plain dict with camelCase keys.
    artifact = {"inlineData": {"mimeType": "text/plain", "data": b"hi"}}
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="d.bin",
            artifact=artifact,
        )
    )
    part = _run(
        svc.load_artifact(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="d.bin"
        )
    )
    assert part is not None
    assert part.inline_data is not None
    assert part.inline_data.data == b"hi"


def test_custom_metadata_round_trips(tmp_path: Path):
    svc = _service(tmp_path)
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            artifact=_text("x"),
            custom_metadata={"source": "unit-test", "n": 3},
        )
    )
    meta = _run(
        svc.get_artifact_version(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
        )
    )
    assert meta is not None
    assert meta.custom_metadata == {"source": "unit-test", "n": 3}
    assert meta.mime_type == "text/plain"
    assert meta.version == 0


# --- listing / versions -----------------------------------------------------


def test_list_versions(tmp_path: Path):
    svc = _service(tmp_path)
    for value in ("a", "b", "c"):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id=_USER,
                session_id=_SESSION,
                filename="a.txt",
                artifact=_text(value),
            )
        )
    versions = _run(
        svc.list_versions(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
        )
    )
    assert sorted(versions) == [0, 1, 2]


def test_list_artifact_versions_sorted_with_metadata(tmp_path: Path):
    svc = _service(tmp_path)
    for value in ("a", "b"):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id=_USER,
                session_id=_SESSION,
                filename="a.txt",
                artifact=_text(value),
            )
        )
    metas = _run(
        svc.list_artifact_versions(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
        )
    )
    assert [m.version for m in metas] == [0, 1]
    assert all(m.canonical_uri for m in metas)


def test_list_artifact_keys_includes_nested_and_user_scope(tmp_path: Path):
    svc = _service(tmp_path)
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            artifact=_text("x"),
        )
    )
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="nested/dir/b.txt",
            artifact=_text("y"),
        )
    )
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            filename="user:pref.json",
            artifact=_text("z"),
        )
    )
    keys = _run(
        svc.list_artifact_keys(app_name=_APP, user_id=_USER, session_id=_SESSION)
    )
    assert keys == ["a.txt", "nested/dir/b.txt", "user:pref.json"]


def test_list_artifact_keys_without_session_only_user_scope(tmp_path: Path):
    svc = _service(tmp_path)
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            artifact=_text("x"),
        )
    )
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            filename="user:pref.json",
            artifact=_text("z"),
        )
    )
    keys = _run(svc.list_artifact_keys(app_name=_APP, user_id=_USER))
    assert keys == ["user:pref.json"]


# --- user-namespaced artifacts ----------------------------------------------


def test_user_scoped_artifact_round_trip(tmp_path: Path):
    svc = _service(tmp_path)
    _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            filename="user:pref.json",
            artifact=_text("saved"),
        )
    )
    # No session id needed to read a user-scoped artifact back.
    part = _run(
        svc.load_artifact(app_name=_APP, user_id=_USER, filename="user:pref.json")
    )
    assert part is not None
    assert part.text == "saved"


def test_session_scoped_save_requires_session(tmp_path: Path):
    svc = _service(tmp_path)
    with pytest.raises(InputValidationError, match="Session ID must be provided"):
        _run(
            svc.save_artifact(
                app_name=_APP, user_id=_USER, filename="a.txt", artifact=_text("x")
            )
        )


# --- delete -----------------------------------------------------------------


def test_delete_removes_all_versions(tmp_path: Path):
    svc = _service(tmp_path)
    for value in ("a", "b"):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id=_USER,
                session_id=_SESSION,
                filename="a.txt",
                artifact=_text(value),
            )
        )
    _run(
        svc.delete_artifact(
            app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
        )
    )
    assert (
        _run(
            svc.list_versions(
                app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
            )
        )
        == []
    )
    assert (
        _run(
            svc.load_artifact(
                app_name=_APP, user_id=_USER, session_id=_SESSION, filename="a.txt"
            )
        )
        is None
    )


# --- path-traversal safety --------------------------------------------------


def test_rejects_traversal_in_user_id(tmp_path: Path):
    svc = _service(tmp_path)
    with pytest.raises(InputValidationError):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id="../evil",
                session_id=_SESSION,
                filename="a.txt",
                artifact=_text("x"),
            )
        )


# --- validation errors ------------------------------------------------------


def test_empty_artifact_rejected(tmp_path: Path):
    svc = _service(tmp_path)
    with pytest.raises(InputValidationError, match="inline_data or text"):
        _run(
            svc.save_artifact(
                app_name=_APP,
                user_id=_USER,
                session_id=_SESSION,
                filename="a.txt",
                artifact=types.Part(),
            )
        )


# --- DI module --------------------------------------------------------------


def test_module_provides_service_from_base_path(tmp_path: Path):
    injector = Injector([ArtifactStorageModule(base_path=str(tmp_path / "art"))])
    svc = injector.get(CloudPathArtifactService)
    assert isinstance(svc, CloudPathArtifactService)


def test_module_provides_singleton(tmp_path: Path):
    injector = Injector([ArtifactStorageModule(base_path=str(tmp_path / "art"))])
    assert injector.get(CloudPathArtifactService) is injector.get(
        CloudPathArtifactService
    )


def test_module_reads_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ARTIFACT_STORAGE_URI_ENV, str(tmp_path / "from-env"))
    svc = Injector([ArtifactStorageModule()]).get(CloudPathArtifactService)
    v = _run(
        svc.save_artifact(
            app_name=_APP,
            user_id=_USER,
            session_id=_SESSION,
            filename="a.txt",
            artifact=_text("x"),
        )
    )
    assert v == 0


def test_module_without_config_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ARTIFACT_STORAGE_URI_ENV, raising=False)
    with pytest.raises(ValueError, match="No artifact storage location"):
        Injector([ArtifactStorageModule()]).get(CloudPathArtifactService)
