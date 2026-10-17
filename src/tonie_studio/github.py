"""Reading tonie dumps out of a GitHub repository.

Collections of Flipper Zero dumps are kept in git repositories, one ``.nfc``
file per tonie in folders by series. Two API calls find them all: one for the
repository's default branch, one for the whole file tree at once. The files
themselves then come from raw.githubusercontent.com, which a public repository
serves without counting against the API's 60 unauthenticated requests an hour;
several hundred dumps would exhaust that limit many times over. A private
repository needs an access token, and with one every file comes through the
API instead, which raw addresses do not accept tokens for.

Only api.github.com and raw.githubusercontent.com are ever asked, whatever the
person types, so nothing here can be pointed at another host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Tuple
from urllib.parse import quote, unquote

import requests

from .i18n import Problem

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

# A dump is a few hundred bytes. Anything far bigger is not one, and reading
# it whole would only cost memory.
LARGEST_DUMP = 64 * 1024

# Long enough for GitHub on a slow day; the tree of a large repository is the
# slowest answer.
TIMEOUT = 30

# What GitHub allows in an owner or repository name.
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_ADDRESS = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)


class RepoError(Problem, ValueError):
    """A repository that cannot be read, or a name that is not one."""


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    branch: str = ""  # empty: the repository's default branch
    subfolder: str = ""  # empty: the whole repository

    @property
    def label(self) -> str:
        where = f"{self.owner}/{self.name}"
        if self.branch:
            where += f"@{self.branch}"
        return f"{where}/{self.subfolder}" if self.subfolder else where


def parse_repo(text: str, branch: str = "", subfolder: str = "") -> Repo:
    """Read ``owner/repo`` or a GitHub address, including a ``/tree/<branch>/<folder>`` one.

    A branch whose name contains a slash cannot be told apart from the folder
    in such an address; ``branch`` names it instead. ``branch`` and
    ``subfolder`` win over what the address says.
    """
    cleaned = text.strip()
    if "://" in cleaned and not _ADDRESS.match(cleaned):
        raise RepoError("github.bad_repo")
    cleaned = _ADDRESS.sub("", cleaned).strip("/")
    parts = [unquote(part) for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise RepoError("github.bad_repo")
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not _NAME.match(owner) or not _NAME.match(name) or name in (".", ".."):
        raise RepoError("github.bad_repo")

    found_branch, found_folder = "", ""
    if len(parts) > 3 and parts[2] == "tree":
        found_branch, found_folder = parts[3], "/".join(parts[4:])
    return Repo(owner, name, branch.strip() or found_branch,
                (subfolder.strip() or found_folder).strip("/"))


Get = Callable[..., "requests.Response"]


def dumps(repo: Repo, token: str = "", get: Get = requests.get) -> List[Tuple[str, str]]:
    """Every ``.nfc`` file in ``repo``, as ``(path, text)`` sorted by path."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tonie-studio"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"{API}/repos/{quote(repo.owner)}/{quote(repo.name)}"

    branch = repo.branch or _api(get, base, headers)["default_branch"]
    tree = _api(get, f"{base}/git/trees/{quote(branch, safe='')}?recursive=1", headers)
    if tree.get("truncated"):
        raise RepoError("github.truncated")

    prefix = f"{repo.subfolder}/" if repo.subfolder else ""
    paths = sorted(entry["path"] for entry in tree.get("tree", [])
                   if entry.get("type") == "blob" and entry["path"].lower().endswith(".nfc")
                   and entry["path"].startswith(prefix))
    if not paths:
        raise RepoError("github.no_dumps", where=repo.label)

    found = []
    for path in paths:
        if token:
            url = f"{base}/contents/{quote(path)}?ref={quote(branch, safe='')}"
            file_headers = dict(headers, Accept="application/vnd.github.raw")
        else:
            url = f"{RAW}/{quote(repo.owner)}/{quote(repo.name)}/{quote(branch, safe='')}/{quote(path)}"
            file_headers = {"User-Agent": "tonie-studio"}
        response = _get(get, url, file_headers)
        if len(response.content) > LARGEST_DUMP:
            raise RepoError("github.too_large", path=path, kilobytes=LARGEST_DUMP // 1024)
        found.append((path, response.text))
    return found


def _api(get: Get, url: str, headers: dict) -> dict:
    return _get(get, url, headers).json()


def _get(get: Get, url: str, headers: dict) -> "requests.Response":
    try:
        response = get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as error:
        raise RepoError("github.unreachable", detail=str(error)) from error
    if response.status_code == 404:
        raise RepoError("github.not_found")
    if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
        reset = response.headers.get("X-RateLimit-Reset", "")
        when = (datetime.fromtimestamp(int(reset), timezone.utc).astimezone().strftime("%H:%M")
                if reset.isdigit() else "?")
        raise RepoError("github.rate_limit", time=when)
    if not 200 <= response.status_code < 300:
        raise RepoError("github.refused", status=response.status_code)
    return response
