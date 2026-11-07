"""Verify published bytes and exercise an index-installed CLI and MCP server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen


MCP_SMOKE = """
import sys
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    with anyio.fail_after(30):
        params = StdioServerParameters(command=sys.argv[1], args=[
            '--state-remote', sys.argv[2], '--cache-dir', sys.argv[3],
            '--actor', 'index-release-smoke',
        ])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.version == sys.argv[4], initialized
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {'get_instructions', 'list_items', 'take', 'finish'} <= names, names
                print('MCP initialize/tools-list passed')

anyio.run(main)
"""


def verify_artifacts(dist: Path, version: str, metadata: dict) -> None:
    if metadata["info"]["version"] != version:
        raise ValueError("index metadata version differs from release")
    expected = {f"tractable-{version}-py3-none-any.whl", f"tractable-{version}.tar.gz"}
    local = {p.name: p for p in dist.iterdir() if p.name.endswith((".whl", ".tar.gz"))}
    published = metadata["urls"]
    if set(local) != expected or {p["filename"] for p in published} != expected or len(published) != 2:
        raise ValueError("expected exactly the release wheel and sdist locally and on the index")
    for asset in published:
        name = asset["filename"]
        if asset.get("yanked"):
            raise ValueError(f"index artifact is yanked: {name}")
        digest = hashlib.sha256(local[name].read_bytes()).hexdigest()
        if asset["digests"]["sha256"] != digest:
            raise ValueError(f"index metadata hash differs from build artifact: {name}")
        downloaded = hashlib.sha256()
        with urlopen(asset["url"], timeout=60) as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                downloaded.update(chunk)
        if downloaded.hexdigest() != digest:
            raise ValueError(f"downloaded index bytes differ from build artifact: {name}")
        print(f"Verified {name}: sha256:{digest}")


def isolated_env(root: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "TRACTABLE_", "GIT_", "PYTHON", "XDG_"))
    }
    env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / ".config"), XDG_CACHE_HOME=str(root / ".cache"))
    return env


def smoke_environment(bin_dir: Path, version: str, root: Path) -> None:
    env = isolated_env(root)
    for name in ("tractable", "tractable-mcp"):
        result = subprocess.run(
            [str(bin_dir / name), "--version"], cwd=root, env=env,
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
        )
        if result.stdout.strip().split()[-1] != version:
            raise ValueError(f"unexpected {name} version: {result.stdout!r}")
        print(result.stdout.strip())
    remote = root / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], env=env, check=True, timeout=30)
    subprocess.run(
        [str(bin_dir / "tractable"), "bootstrap", "--state-remote", str(remote),
         "--cache-dir", str(root / "cache")],
        cwd=root, env=env, check=True, timeout=30,
    )
    subprocess.run(
        [str(bin_dir / "python"), "-I", "-c", MCP_SMOKE, str(bin_dir / "tractable-mcp"),
         str(remote), str(root / "cache"), version],
        cwd=root, env=env, check=True, timeout=60,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("dist", type=Path)
    args = parser.parse_args()
    with urlopen(f"https://pypi.org/pypi/tractable/{args.version}/json", timeout=60) as response:
        metadata = json.load(response)
    verify_artifacts(args.dist, args.version, metadata)
    with tempfile.TemporaryDirectory(prefix="tractable-index-") as temporary:
        root = Path(temporary)
        env = isolated_env(root)
        python = root / "venv" / "bin" / "python"
        subprocess.run(
            ["uv", "--no-config", "--no-cache", "venv", "--python", sys.executable, str(python.parent.parent)],
            cwd=root, env=env, check=True, timeout=60,
        )
        subprocess.run(
            ["uv", "--no-config", "--no-cache", "pip", "install", "--python", str(python),
             "--default-index", "https://pypi.org/simple", f"tractable[mcp]=={args.version}"],
            cwd=root, env=env, check=True, timeout=180,
        )
        smoke_environment(python.parent, args.version, root)
    print(f"Index distribution verified: tractable {args.version}")


if __name__ == "__main__":
    main()
