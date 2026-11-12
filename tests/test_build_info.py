"""Build attribution stays local, stable for a process, and free of credentials."""

import asyncio
import json
import re

from meta_ads_mcp.build_info import BUILD_IDENTITY
from meta_ads_mcp.tools import utility


def test_capabilities_exposes_nonsecret_build_identity(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "never-emit-this-token")
    result = asyncio.run(utility.get_capabilities())
    identity = result["server"]["build"]
    assert identity == BUILD_IDENTITY
    assert re.fullmatch(r"[0-9a-f]{64}", identity["source_sha256"])
    assert identity["package_versions"]["fastmcp"] != "not-installed"
    assert "never-emit-this-token" not in json.dumps(result)
    assert "/Users/" not in json.dumps(identity)
