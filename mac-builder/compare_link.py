"""Turn a git remote URL into a GitHub compare/merge link.

Pure text: no git call, no network and no Discord. The remote URL comes from a
checkout and may carry a credential, so anything before an ``@`` is dropped
before a URL is built, and a remote that is not GitHub yields ``None`` instead
of a link that would 404.
"""

from __future__ import annotations

import re
from urllib.parse import quote

_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://")
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
# Branch names keep their slashes; anything else a hostile string carries is
# percent-encoded so it cannot break the URL it is pasted into.
_BRANCH_SAFE = "/._-"


def _owner_repo(remote_url: str) -> str | None:
    """``OWNER/REPO`` from a GitHub remote, or None when it is not one."""
    value = (remote_url or "").strip()
    if not value or any(char in value for char in "?#\n\r\t "):
        return None

    scheme = _SCHEME_RE.match(value)
    if scheme:
        rest = value[scheme.end():]
        # Drop https://user:token@... and ssh://git@... before parsing.
        rest = rest.rsplit("@", 1)[-1]
        host, separator, path = rest.partition("/")
        if not separator:
            return None
    elif "@" in value:
        # scp-like: git@github.com:OWNER/REPO(.git)
        host, separator, path = value.rsplit("@", 1)[-1].partition(":")
        if not separator:
            return None
    else:
        return None

    # A port is not part of the host name.
    host = host.split(":", 1)[0].strip().lower()
    if host not in _GITHUB_HOSTS:
        return None

    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def compare_url(remote_url: str, base: str, branch: str) -> str | None:
    """A GitHub 'compare and open a pull request' link, or None.

    Accepts ``https://github.com/OWNER/REPO(.git)``,
    ``git@github.com:OWNER/REPO(.git)`` and
    ``ssh://git@github.com/OWNER/REPO(.git)``, with or without credentials.
    Any other host, an unparseable remote, or an empty branch returns None.
    """
    owner_repo = _owner_repo(remote_url)
    if owner_repo is None:
        return None
    base = (base or "").strip()
    branch = (branch or "").strip()
    if not base or not branch:
        return None
    return (
        f"https://github.com/{owner_repo}/compare/"
        f"{quote(base, safe=_BRANCH_SAFE)}...{quote(branch, safe=_BRANCH_SAFE)}?expand=1"
    )
