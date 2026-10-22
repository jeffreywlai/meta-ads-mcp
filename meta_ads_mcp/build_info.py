"""Non-secret build identity for attributing reports to the running package."""

from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _build_identity() -> dict[str, object]:
    package_root = Path(__file__).parent
    digest = sha256()
    for source in sorted(package_root.rglob("*.py")):
        digest.update(source.relative_to(package_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    versions = {}
    for package in ("meta-ads-fastmcp", "fastmcp", "mcp", "httpx"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "not-installed"
    return {"source_sha256": digest.hexdigest(), "package_versions": versions}


# Snapshot once at import: changing files requires a process restart to reload.
BUILD_IDENTITY = _build_identity()
