"""
Write notes to the user's Obsidian vault.

Two implementations chosen at runtime by `from_env()`:

- LocalVaultWriter: direct filesystem writes when `OBSIDIAN_VAULT_PATH` env is
  set AND points to an existing directory. Used by OpenJarvis running on the
  user's local machine — Obsidian sees the note instantly.
- ObsidianMindWriter: HTTPS POST to the `obsidian-mind` Railway service when
  `OBSIDIAN_MIND_URL` is set. Used by fj2 running on Railway (no local FS).
- NullVaultWriter: when neither is configured — write_note is a no-op.

The chooser prefers LocalVaultWriter if both are configured.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx


class VaultWriter(ABC):
    @abstractmethod
    async def write_note(
        self, rel_path: str, content: str, *, overwrite: bool = False
    ) -> None:
        """`rel_path` is vault-relative (e.g. "brain/My Note.md"). Creates
        parent dirs as needed. Refuses to overwrite an existing file unless
        `overwrite=True`."""


class LocalVaultWriter(VaultWriter):
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"vault root does not exist: {self.root}")

    async def write_note(
        self, rel_path: str, content: str, *, overwrite: bool = False
    ) -> None:
        target = (self.root / rel_path).resolve()
        # Path-traversal guard: target must stay inside the vault root.
        if not str(target).startswith(str(self.root)):
            raise ValueError(f"path traversal blocked: {rel_path}")
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


class ObsidianMindWriter(VaultWriter):
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def write_note(
        self, rel_path: str, content: str, *, overwrite: bool = False
    ) -> None:
        # The remote endpoint accepts POST /api/notes/<path> with body {content}.
        # No native "no-overwrite" mode, so we check existence first if needed.
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if not overwrite:
                resp = await client.get(
                    f"{self.base_url}/api/notes/{rel_path}", headers=headers
                )
                if resp.status_code == 200:
                    raise FileExistsError(rel_path)
            resp = await client.post(
                f"{self.base_url}/api/notes/{rel_path}",
                json={"content": content},
                headers=headers,
            )
            resp.raise_for_status()


class NullVaultWriter(VaultWriter):
    async def write_note(
        self, rel_path: str, content: str, *, overwrite: bool = False
    ) -> None:
        return None


def from_env() -> VaultWriter:
    """Pick the appropriate writer based on env. Local FS wins if both are set."""
    local = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    if local and Path(local).is_dir():
        return LocalVaultWriter(Path(local))
    remote = os.environ.get("OBSIDIAN_MIND_URL", "").strip()
    if remote:
        return ObsidianMindWriter(
            base_url=remote,
            token=os.environ.get("OBSIDIAN_MIND_TOKEN", "").strip() or None,
        )
    return NullVaultWriter()
