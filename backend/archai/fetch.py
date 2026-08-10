from __future__ import annotations

import pathlib
import shutil
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit


IGNORE_DIRS = {
    "build",
    "target",
    "out",
    "bin",
    ".git",
    "generated",
    "gen",
    "node_modules",
    ".gradle",
    ".idea",
}


def _with_token(url: str, token: str | None, username: str | None = None) -> str:
    """Return a git HTTPS URL with optional token credentials embedded.

    Inputs:
        url: The original repository URL, for example
            "https://freelab.oraclecorp.com/group/repo.git".
        token: A personal access token. If this is empty, the original URL is
            returned unchanged.
        username: The HTTPS username to pair with the token. When not provided,
            "oauth2" is used because GitLab accepts that username for token auth.

    Output:
        A URL shaped as "https://username:token@host/path.git" for HTTPS repos,
        or the original URL for non-HTTPS repos.

    Working:
        Git treats the value before "@" as credentials. The token must be placed
        after a colon so it is sent as the password, not as the username. Both
        username and token are URL-encoded so special characters inside tokens do
        not break the clone URL.
    """
    if not token:
        return url
    if not url.startswith("https://"):
        return url

    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    safe_username = quote(username or "oauth2", safe="")
    safe_token = quote(token, safe="")
    return urlunsplit(
        (parts.scheme, f"{safe_username}:{safe_token}@{netloc}", parts.path, parts.query, parts.fragment)
    )


def _redact_token(value: str, token: str | None) -> str:
    """Remove a token from loggable command output.

    Inputs:
        value: Any stdout/stderr/error text that may contain the raw or encoded
            access token.
        token: The sensitive token to remove. If absent, the text is returned as
            is.

    Output:
        The same text with token occurrences replaced by "***".

    Working:
        Git can echo either the raw token or a URL-encoded token depending on the
        failure path. This helper redacts both forms before an exception message
        is raised to the caller.
    """
    if not token:
        return value
    return value.replace(token, "***").replace(quote(token, safe=""), "***")


def clone_repo(
    url: str,
    token: str | None = None,
    dest: str = "/tmp/archai_repo",
    username: str | None = None,
) -> str:
    """Clone a git repository and return the local destination path.

    Inputs:
        url: The public HTTPS git URL for the repository.
        token: Optional personal access token for private repositories.
        dest: Local directory where the repository should be cloned. Any existing
            directory at this path is removed before cloning.
        username: Optional HTTPS username used together with the token. If not
            supplied, token auth defaults to the username "oauth2".

    Output:
        A string path to the cloned repository directory.

    Working:
        The function builds a credentialed clone URL only in memory, runs
        "git clone --depth 1" for a shallow clone, and captures output so the
        token is never printed by git. If git fails, stdout/stderr are redacted
        before a RuntimeError is raised.
    """
    dest_path = pathlib.Path(dest)
    if dest_path.exists():
        shutil.rmtree(dest_path, ignore_errors=True)

    clone_url = _with_token(url, token, username)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(dest_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = _redact_token(exc.stderr or "", token)
        stdout = _redact_token(exc.stdout or "", token)
        raise RuntimeError(f"git clone failed\nstdout: {stdout}\nstderr: {stderr}") from None

    return str(dest_path)


def remote_head_revision(
    url: str,
    token: str | None = None,
    username: str | None = None,
) -> str | None:
    """Return the remote HEAD commit without cloning, or ``None`` on failure.

    This probe is used only when complete local analysis artifacts already
    exist. A matching commit allows ArchAI to reuse those exact artifacts;
    failure deliberately falls through to the established clone-and-analyze
    path so authentication and network behavior remain backward compatible.
    """
    remote_url = _with_token(url, token, username)
    try:
        completed = subprocess.run(
            ["git", "ls-remote", remote_url, "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    first_line = (completed.stdout or "").strip().splitlines()
    if not first_line:
        return None
    fields = first_line[0].split()
    revision = fields[0].strip() if fields else ""
    return revision if len(revision) >= 7 else None


def list_java_files(root: str | pathlib.Path):
    """Yield Java source files that should be analyzed.

    Inputs:
        root: Repository root directory, either as a string or Path.

    Output:
        An iterator of Path objects for ".java" files under the root.

    Working:
        The repository is searched recursively. Build outputs, generated folders,
        dependency folders, IDE metadata, and git metadata are skipped so the
        analyzer works on source files rather than compiled/generated artifacts.
        "package-info.java" and "module-info.java" are skipped because they do
        not contain top-level type declarations for the graph.
    """
    root_path = pathlib.Path(root)
    for path in root_path.rglob("*.java"):
        rel_parts = path.relative_to(root_path).parts
        if any(part in IGNORE_DIRS for part in rel_parts):
            continue
        if path.name in {"package-info.java", "module-info.java"}:
            continue
        yield path
