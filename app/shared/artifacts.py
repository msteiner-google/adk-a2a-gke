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

"""A cloud-agnostic ADK artifact service backed by `cloudpathlib`.

`CloudPathArtifactService` implements ADK's `BaseArtifactService` on top of any
storage backend `cloudpathlib` speaks — Amazon S3 (`s3://`), Google Cloud Storage
(`gs://`), Azure Blob Storage (`az://`), or the local filesystem (a plain path) —
by pointing it at a *base path*:

    from .shared.artifacts import CloudPathArtifactService

    service = CloudPathArtifactService("s3://my-bucket/artifacts")
    service = CloudPathArtifactService("gs://my-bucket/artifacts")
    service = CloudPathArtifactService("/var/lib/agent/artifacts")  # local

Pass it to the ADK runner / serving wherever a `BaseArtifactService` is expected.
Because the base path is resolved with `cloudpathlib.AnyPath`, the *same* code
runs against every backend (a plain path yields a local `pathlib.Path`, so tests
need no network / credentials). Backend credentials follow each provider's usual
discovery (boto3 for S3, ADC for GCS, ...); to customize them, build a
`cloudpathlib` `CloudPath` with an explicit client and pass that as `base_path`.

Storage layout (mirrors `GcsArtifactService`'s key scheme). Each version is a
single self-describing JSON envelope object named by its integer version, so a
version listing is just a directory listing and every write is atomic:

    <base>/{app_name}/{user_id}/{session_id}/{filename}/{version}   # session-scoped
    <base>/{app_name}/{user_id}/user/{filename}/{version}           # "user:"-namespaced

The envelope stores the payload (inline bytes base64-encoded, text, or a
`file_data` URI reference), its mime type, any custom metadata, and a creation
timestamp — everything needed to reconstruct the `types.Part` and its
`ArtifactVersion` metadata without relying on backend-specific object metadata
(which is not portable across S3 / GCS / Azure).

`ArtifactStorageModule` wires the service through `injector`, resolving the base
path from a constructor override or the `ARTIFACT_STORAGE_URI` env var:

    from injector import Injector
    from .shared.artifacts import ArtifactStorageModule, CloudPathArtifactService

    service = Injector([ArtifactStorageModule()]).get(CloudPathArtifactService)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from cloudpathlib import AnyPath, CloudPath
from google.adk.artifacts import artifact_util
from google.adk.artifacts.base_artifact_service import (
    ArtifactVersion,
    BaseArtifactService,
    ensure_part,
)
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types
from injector import Module, provider, singleton
from typing_extensions import override  # 3.11-safe form (typing.override is 3.12+)

# Env var naming the storage location (e.g. `s3://bucket/prefix`,
# `gs://bucket/prefix`, or a local path) when the DI module is used without an
# explicit constructor override.
ARTIFACT_STORAGE_URI_ENV = "ARTIFACT_STORAGE_URI"

# Fallback mime type for inline payloads persisted without one, since
# `types.Part.from_bytes` requires a (non-None) mime type on load.
_DEFAULT_MIME_TYPE = "application/octet-stream"

# The user-namespace prefix: filenames starting with this are scoped to the user
# across all their sessions rather than to a single session.
_USER_NAMESPACE_PREFIX = "user:"

# A storage-agnostic path: a cloud object path or a local filesystem path.
StoragePath = CloudPath | Path


class CloudPathArtifactService(BaseArtifactService):
    """An ADK artifact service backed by any `cloudpathlib` storage backend.

    One instance is bound to a single base path (bucket + optional prefix, or a
    local directory); every artifact is stored beneath it using the same key
    scheme as `GcsArtifactService`, so switching backends is purely a matter of
    the base-path scheme.
    """

    def __init__(self, base_path: str | os.PathLike[str] | StoragePath) -> None:
        """Initialize the service against a storage base path.

        Args:
            base_path: The root under which artifacts are stored. A string or
                `os.PathLike` is resolved with `cloudpathlib.AnyPath` (a cloud
                URI like `s3://bucket/prefix` or `gs://bucket/prefix` yields a
                `CloudPath`; any other path yields a local `pathlib.Path`). An
                already-constructed `CloudPath`/`Path` (e.g. one carrying a
                custom client for credentials) is used as-is.
        """
        if isinstance(base_path, (CloudPath, Path)):
            self._root: StoragePath = base_path
        else:
            self._root = AnyPath(os.fspath(base_path))
        self._is_cloud = isinstance(self._root, CloudPath)

    # --- path helpers -------------------------------------------------------

    def _file_has_user_namespace(self, filename: str) -> bool:
        """Return True if `filename` is user-scoped (starts with `user:`)."""
        return filename.startswith(_USER_NAMESPACE_PREFIX)

    def _artifact_dir(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None,
    ) -> StoragePath:
        """Return the directory holding every version of one artifact.

        Args:
            app_name: The application name.
            user_id: The user id.
            filename: The artifact filename (may be `user:`-namespaced or
                contain `/` separators).
            session_id: The session id, required for non-`user:` artifacts.

        Returns:
            The storage path of the artifact's version directory.

        Raises:
            InputValidationError: If a path segment is unsafe, or a session id is
                missing for a session-scoped artifact.
        """
        artifact_util.validate_path_segment(app_name, "app_name")
        artifact_util.validate_path_segment(user_id, "user_id")
        if self._file_has_user_namespace(filename):
            return self._root / app_name / user_id / "user" / filename
        if session_id is None:
            raise InputValidationError(
                "Session ID must be provided for session-scoped artifacts."
            )
        artifact_util.validate_path_segment(session_id, "session_id")
        return self._root / app_name / user_id / session_id / filename

    def _version_path(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        version: int,
        session_id: str | None,
    ) -> StoragePath:
        """Return the envelope path for a specific artifact version."""
        directory = self._artifact_dir(app_name, user_id, filename, session_id)
        return directory / str(version)

    def _ensure_parent(self, path: StoragePath) -> None:
        """Create parent directories for local paths (a no-op for cloud paths).

        Cloud object stores have no real directories, so writing a blob creates
        any implied prefixes automatically; the local filesystem needs the
        parent directory to exist first.
        """
        if not self._is_cloud:
            path.parent.mkdir(parents=True, exist_ok=True)

    # --- envelope (de)serialization ----------------------------------------

    def _write_envelope(self, path: StoragePath, envelope: dict[str, Any]) -> None:
        """Serialize and persist an artifact-version envelope as JSON."""
        self._ensure_parent(path)
        path.write_text(json.dumps(envelope))

    def _build_envelope(
        self,
        version: int,
        artifact: types.Part,
        custom_metadata: dict[str, Any] | None,
        app_name: str,
        user_id: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Build the JSON-serializable envelope describing an artifact version.

        Args:
            version: The version number being written.
            artifact: The normalized `types.Part` to persist.
            custom_metadata: Optional user metadata to store with the version.
            app_name: The application name (for reference-scope validation).
            user_id: The user id (for reference-scope validation).
            session_id: The session id (for reference-scope validation).

        Returns:
            A JSON-serializable dict capturing the payload and its metadata.

        Raises:
            InputValidationError: If the artifact has no persistable payload, or
                carries an invalid/out-of-scope artifact reference.
        """
        envelope: dict[str, Any] = {
            "version": version,
            "create_time": time.time(),
            "custom_metadata": custom_metadata or {},
        }
        if artifact.inline_data:
            envelope["kind"] = "inline"
            envelope["mime_type"] = artifact.inline_data.mime_type
            data = artifact.inline_data.data or b""
            envelope["data"] = base64.b64encode(data).decode("ascii")
            if artifact.inline_data.display_name:
                envelope["display_name"] = artifact.inline_data.display_name
        elif artifact.text is not None:
            envelope["kind"] = "text"
            envelope["mime_type"] = "text/plain"
            envelope["text"] = artifact.text
        elif artifact.file_data:
            file_uri = artifact.file_data.file_uri
            if not file_uri:
                raise InputValidationError("Artifact file_data must have a file_uri.")
            if artifact_util.is_artifact_ref(artifact):
                parsed_uri = artifact_util.parse_artifact_uri(file_uri)
                if not parsed_uri:
                    raise InputValidationError(
                        f"Invalid artifact reference URI: {file_uri}"
                    )
                artifact_util.validate_artifact_reference_scope(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                    parsed_uri=parsed_uri,
                )
            envelope["kind"] = "file"
            envelope["file_uri"] = file_uri
            envelope["mime_type"] = artifact.file_data.mime_type
        else:
            raise InputValidationError("Artifact must have either inline_data or text.")
        return envelope

    def _part_from_envelope(
        self,
        envelope: dict[str, Any],
        app_name: str,
        user_id: str,
        session_id: str | None,
    ) -> types.Part | None:
        """Reconstruct a `types.Part` from a persisted envelope.

        Args:
            envelope: The decoded envelope dict.
            app_name: The requesting application name (for reference scoping).
            user_id: The requesting user id (for reference scoping).
            session_id: The requesting session id (for reference scoping).

        Returns:
            The reconstructed `types.Part`, or the resolved target `Part` when
            the envelope is an `artifact://` reference.

        Raises:
            InputValidationError: If the envelope holds an invalid/out-of-scope
                artifact reference.
        """
        kind = envelope.get("kind")
        if kind == "text":
            return types.Part(text=envelope["text"])
        if kind == "file":
            file_uri = envelope["file_uri"]
            if file_uri.startswith("artifact://"):
                parsed_uri = artifact_util.parse_artifact_uri(file_uri)
                if not parsed_uri:
                    raise InputValidationError(
                        f"Invalid artifact reference URI: {file_uri}"
                    )
                artifact_util.validate_artifact_reference_scope(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                    parsed_uri=parsed_uri,
                )
                return self._load_artifact(
                    parsed_uri.app_name,
                    parsed_uri.user_id,
                    parsed_uri.session_id,
                    parsed_uri.filename,
                    parsed_uri.version,
                )
            return types.Part(
                file_data=types.FileData(
                    file_uri=file_uri,
                    mime_type=envelope.get("mime_type"),
                )
            )
        # Default: inline binary payload.
        data = base64.b64decode(envelope["data"])
        mime_type = envelope.get("mime_type") or _DEFAULT_MIME_TYPE
        display_name = envelope.get("display_name")
        if display_name:
            return types.Part(
                inline_data=types.Blob(
                    mime_type=mime_type,
                    data=data,
                    display_name=display_name,
                )
            )
        return types.Part.from_bytes(data=data, mime_type=mime_type)

    # --- synchronous core ---------------------------------------------------

    def _list_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
    ) -> list[int]:
        """Return every stored version number for an artifact (unsorted)."""
        directory = self._artifact_dir(app_name, user_id, filename, session_id)
        if not directory.exists():
            return []
        versions = []
        for child in directory.iterdir():
            if child.name.isdigit():
                versions.append(int(child.name))
        return versions

    def _save_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        artifact: types.Part | dict[str, Any],
        custom_metadata: dict[str, Any] | None,
    ) -> int:
        """Persist a new version of an artifact, returning its version number."""
        part = ensure_part(artifact)
        versions = self._list_versions(app_name, user_id, session_id, filename)
        version = 0 if not versions else max(versions) + 1
        envelope = self._build_envelope(
            version, part, custom_metadata, app_name, user_id, session_id
        )
        path = self._version_path(app_name, user_id, filename, version, session_id)
        self._write_envelope(path, envelope)
        return version

    def _load_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        version: int | None,
    ) -> types.Part | None:
        """Load a single artifact version (the latest when `version` is None)."""
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                return None
            version = max(versions)
        path = self._version_path(app_name, user_id, filename, version, session_id)
        if not path.exists():
            return None
        envelope = json.loads(path.read_text())
        return self._part_from_envelope(envelope, app_name, user_id, session_id)

    def _filenames_under(self, scope_dir: StoragePath) -> set[str]:
        """Collect artifact filenames stored beneath a scope directory.

        A filename may itself contain `/` separators, so it is reconstructed as
        everything between the scope directory and the trailing version segment.
        """
        if not scope_dir.exists():
            return set()
        base_depth = len(scope_dir.parts)
        filenames: set[str] = set()
        for leaf in scope_dir.rglob("*"):
            if not leaf.is_file():
                continue
            # Path under the scope dir: `<filename...>/<version>`. Slicing parts
            # (rather than `relative_to`) keeps a uniform type across the
            # CloudPath / local-Path union that `ty` accepts.
            parts = leaf.parts[base_depth:]
            if len(parts) < 2 or not parts[-1].isdigit():
                continue
            filenames.add("/".join(parts[:-1]))
        return filenames

    def _list_artifact_keys(
        self, app_name: str, user_id: str, session_id: str | None
    ) -> list[str]:
        """List session-scoped and user-scoped artifact filenames."""
        artifact_util.validate_path_segment(app_name, "app_name")
        artifact_util.validate_path_segment(user_id, "user_id")
        filenames: set[str] = set()
        if session_id is not None:
            artifact_util.validate_path_segment(session_id, "session_id")
            session_dir = self._root / app_name / user_id / session_id
            filenames |= self._filenames_under(session_dir)
        user_dir = self._root / app_name / user_id / "user"
        filenames |= self._filenames_under(user_dir)
        return sorted(filenames)

    def _delete_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
    ) -> None:
        """Delete every version of an artifact."""
        for version in self._list_versions(app_name, user_id, session_id, filename):
            path = self._version_path(app_name, user_id, filename, version, session_id)
            if path.exists():
                path.unlink()

    def _artifact_version_meta(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        version: int,
    ) -> ArtifactVersion | None:
        """Build the `ArtifactVersion` metadata for one stored version."""
        path = self._version_path(app_name, user_id, filename, version, session_id)
        if not path.exists():
            return None
        envelope = json.loads(path.read_text())
        return ArtifactVersion(
            version=version,
            canonical_uri=str(path),
            create_time=envelope.get("create_time", time.time()),
            mime_type=envelope.get("mime_type"),
            custom_metadata=envelope.get("custom_metadata") or {},
        )

    def _get_artifact_version(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
        version: int | None,
    ) -> ArtifactVersion | None:
        """Return metadata for one version (the latest when `version` is None)."""
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                return None
            version = max(versions)
        return self._artifact_version_meta(
            app_name, user_id, session_id, filename, version
        )

    def _list_artifact_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: str | None,
        filename: str,
    ) -> list[ArtifactVersion]:
        """Return metadata for every version, ordered by version number."""
        metas = []
        for version in self._list_versions(app_name, user_id, session_id, filename):
            meta = self._artifact_version_meta(
                app_name, user_id, session_id, filename, version
            )
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.version)
        return metas

    # --- async BaseArtifactService surface ----------------------------------

    @override
    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: types.Part | dict[str, Any],
        session_id: str | None = None,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Save a new artifact version off the event loop (see base class)."""
        return await asyncio.to_thread(
            self._save_artifact,
            app_name,
            user_id,
            session_id,
            filename,
            artifact,
            custom_metadata,
        )

    @override
    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
        version: int | None = None,
    ) -> types.Part | None:
        """Load an artifact version off the event loop (see base class)."""
        return await asyncio.to_thread(
            self._load_artifact,
            app_name,
            user_id,
            session_id,
            filename,
            version,
        )

    @override
    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: str | None = None
    ) -> list[str]:
        """List artifact filenames off the event loop (see base class)."""
        return await asyncio.to_thread(
            self._list_artifact_keys,
            app_name,
            user_id,
            session_id,
        )

    @override
    async def delete_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
    ) -> None:
        """Delete every version of an artifact off the event loop (base class)."""
        return await asyncio.to_thread(
            self._delete_artifact,
            app_name,
            user_id,
            session_id,
            filename,
        )

    @override
    async def list_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
    ) -> list[int]:
        """List an artifact's version numbers off the event loop (base class)."""
        return await asyncio.to_thread(
            self._list_versions,
            app_name,
            user_id,
            session_id,
            filename,
        )

    @override
    async def list_artifact_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
    ) -> list[ArtifactVersion]:
        """List version metadata off the event loop (see base class)."""
        return await asyncio.to_thread(
            self._list_artifact_versions,
            app_name,
            user_id,
            session_id,
            filename,
        )

    @override
    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
        version: int | None = None,
    ) -> ArtifactVersion | None:
        """Get one version's metadata off the event loop (see base class)."""
        return await asyncio.to_thread(
            self._get_artifact_version,
            app_name,
            user_id,
            session_id,
            filename,
            version,
        )


class ArtifactStorageModule(Module):
    """Injector module providing a `CloudPathArtifactService` singleton.

    The base path is taken from the constructor override, then the
    `ARTIFACT_STORAGE_URI` env var. Install it when building an `Injector`:

        from injector import Injector
        from .shared.artifacts import ArtifactStorageModule, CloudPathArtifactService

        service = Injector([ArtifactStorageModule()]).get(CloudPathArtifactService)

    Unlike `SecretModule`, this module is self-contained and can be installed on
    its own (it does not depend on `ModelModule`'s bindings).
    """

    def __init__(self, base_path: str | None = None) -> None:
        """Configure the storage base path.

        Args:
            base_path: The storage location (e.g. `s3://bucket/prefix`,
                `gs://bucket/prefix`, or a local path). Defaults to the
                `ARTIFACT_STORAGE_URI` env var when omitted.
        """
        self._base_path = base_path

    @singleton
    @provider
    def provide_artifact_service(self) -> CloudPathArtifactService:
        """Provide the (singleton) cloud-agnostic artifact service.

        Returns:
            A `CloudPathArtifactService` bound to the resolved base path.

        Raises:
            ValueError: If no base path is configured.
        """
        base_path = self._base_path or os.environ.get(ARTIFACT_STORAGE_URI_ENV)
        if not base_path:
            raise ValueError(
                "No artifact storage location configured: set "
                f"{ARTIFACT_STORAGE_URI_ENV} (e.g. 's3://bucket/prefix', "
                "'gs://bucket/prefix', or a local path) or pass base_path to "
                "ArtifactStorageModule."
            )
        return CloudPathArtifactService(base_path)
