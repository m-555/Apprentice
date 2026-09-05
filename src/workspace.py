"""Task-owned worktrees and byte-preserving, conflict-checked delivery.

A worktree is edit isolation, not an OS sandbox. Commands still need permission.
Never reset the user's checkout, index, or branch; never apply with --3way.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


SKIP = {".git", "node_modules", ".venv", ".aider-venv", "__pycache__",
        ".pytest_cache", "Binaries", "Intermediate"}


def git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(repo), *args],
                            input=data, stdin=subprocess.DEVNULL if data is None else None,
                            capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def safe_path(root: Path, name: str) -> Path:
    candidate = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts or ".git" in Path(name).parts:
        raise ValueError(f"Unsafe task path: {name}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes repository: {name}")
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError(f"Symlinks require manual review: {name}")
        cursor = cursor.parent
    return candidate


def excluded(name: str) -> bool:
    return any(p in SKIP or p.startswith(".aider") for p in Path(name).parts)


def state(root: Path, name: str) -> dict:
    p = safe_path(root, name)
    if not p.exists():
        return {"data": None, "mode": None}
    if not p.is_file():
        raise ValueError(f"Not a regular file: {name}")
    return {"data": base64.b64encode(p.read_bytes()).decode("ascii"),
            "mode": p.stat().st_mode & 0o777}


class RepositoryLock:
    """Cross-process lock released by the OS even when a client crashes."""
    def __init__(self, repo: Path, outputs: Path):
        folder = outputs / ".locks"
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(os.path.normcase(str(repo.resolve())).encode()).hexdigest()
        self.stream = (folder / key).open("a+b")
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ValueError("Another Apprentice task is using this repository. Finish or stop it first.") from exc

    def close(self):
        self.stream.close()


def scan(root: Path, max_bytes: int = 256 * 1024 * 1024) -> dict[str, dict]:
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    result, total = {}, 0
    for name in sorted(set(names.decode("utf-8").split("\0")) - {""}):
        if excluded(name):
            continue
        p = safe_path(root, name)
        if p.is_file():
            total += p.stat().st_size
            if total > max_bytes:
                raise ValueError("Task snapshot exceeds the configured byte limit. Narrow the repository or raise workspace.max_bytes.")
        result[name] = state(root, name)
    return result


def restore(root: Path, name: str, value: dict) -> None:
    p = safe_path(root, name)
    if value["data"] is None:
        p.unlink(missing_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(value["data"]))
        os.chmod(p, value["mode"])


class Workspace:
    def __init__(self, repo: str, outputs: Path, max_bytes: int = 256 * 1024 * 1024, session_id: str = ""):
        self.repo = Path(repo).resolve()
        self.outputs = outputs.resolve()
        self.max_bytes = max_bytes
        self.base: dict[str, dict] = {}
        self.directory: Path | None = None
        self.temp: Path | None = None
        self.lock: RepositoryLock | None = None
        self.session_id = session_id

    def __enter__(self):
        # Fail clearly for non-git/empty repos; never initialise the user's repository.
        git(self.repo, "rev-parse", "HEAD")
        self.lock = RepositoryLock(self.repo, self.outputs)
        try:
            self.base = scan(self.repo, self.max_bytes)
            if self.session_id:
                # OpenCode binds native sessions to a directory. Recreate a fresh
                # snapshot at the same owned path each turn, never reuse stale files.
                key = hashlib.sha256((str(self.repo) + "\0" + self.session_id).encode()).hexdigest()
                temporary = Path(tempfile.gettempdir()) / ("apprentice-session-" + key)
                temporary.mkdir(exist_ok=False)
                self.temp = temporary
            else:
                self.temp = Path(tempfile.mkdtemp(prefix="apprentice-task-"))
            self.directory = self.temp / "worktree"
            git(self.repo, "worktree", "add", "--detach", str(self.directory), "HEAD")
            for name, value in self.base.items():
                restore(self.directory, name, value)
            git(self.directory, "add", "-A")
            self.tree = git(self.directory, "write-tree").decode().strip()
            return self
        except BaseException:
            self.close()
            raise

    def changes(self) -> dict:
        assert self.directory
        after = scan(self.directory, self.max_bytes)
        absent = {"data": None, "mode": None}
        return {name: {"before": self.base.get(name, absent), "after": after.get(name, absent)}
                for name in sorted(self.base.keys() | after.keys())
                if self.base.get(name, absent) != after.get(name, absent)}

    def deliver(self, task_id: str, apply: bool) -> dict:
        assert self.directory
        changes = self.changes()
        self.outputs.mkdir(parents=True, exist_ok=True)
        artifact = self.outputs / task_id
        artifact.mkdir(exist_ok=False)
        git(self.directory, "add", "-A")
        patch = git(self.directory, "diff", "--cached", "--binary", self.tree, "--", *changes) if changes else b""
        patch_path = artifact / "changes.patch"
        patch_path.write_bytes(patch)
        manifest = {"repo": str(self.repo), "changes": changes, "applied": False}
        manifest_path = artifact / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        conflicts = [name for name, values in changes.items()
                     if state(self.repo, name) != values["before"]]
        if apply and changes and not conflicts:
            # git apply's default failure is atomic; do not use --reject/--3way.
            git(self.repo, "apply", "--check", str(patch_path))
            git(self.repo, "apply", str(patch_path))
            manifest["applied"] = True
            # Capture actual platform file modes after Git's application.
            for name, values in changes.items():
                values["after"] = state(self.repo, name)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return {"patch_path": str(patch_path), "manifest_path": str(manifest_path),
                "files_changed": list(changes), "applied": manifest["applied"],
                "apply_error": "Files changed since task start: " + ", ".join(conflicts) if conflicts else ""}

    def close(self):
        try:
            if self.directory and self.directory.exists():
                # Only this object's freshly allocated worktree can be removed.
                git(self.repo, "worktree", "remove", "--force", str(self.directory))
            if self.temp and self.temp.exists():
                self.temp.rmdir()
        finally:
            if self.lock:
                self.lock.close()
                self.lock = None

    def __exit__(self, *_):
        self.close()


def undo(manifest_path: Path, repo: str) -> list[str]:
    record = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(repo).resolve()
    if Path(record["repo"]).resolve() != root or not record["applied"]:
        raise ValueError("This task has no applied changes in this repository.")
    conflicts = [name for name, values in record["changes"].items()
                 if state(root, name) != values["after"]]
    if conflicts:
        raise ValueError("Undo would overwrite newer edits: " + ", ".join(conflicts))
    for name, values in record["changes"].items():
        restore(root, name, values["before"])
    record["applied"] = False
    manifest_path.write_text(json.dumps(record), encoding="utf-8")
    return list(record["changes"])
