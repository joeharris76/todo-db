"""Integrity checks for the release's index distribution gate."""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

import pytest


verify_artifacts = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "verify_index_release.py")
)["verify_artifacts"]


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, dict]:
    dist = tmp_path / "dist"
    dist.mkdir()
    index = tmp_path / "index"
    index.mkdir()
    urls = []
    for filename in ("todo_db-0.14.0-py3-none-any.whl", "todo_db-0.14.0.tar.gz"):
        content = filename.encode("utf-8")
        (dist / filename).write_bytes(content)
        published = index / filename
        published.write_bytes(content)
        urls.append({
            "filename": filename, "url": published.as_uri(), "yanked": False,
            "digests": {"sha256": hashlib.sha256(content).hexdigest()},
        })
    return dist, {"info": {"version": "0.14.0"}, "urls": urls}


def test_matching_release_bytes_pass(release: tuple[Path, dict]) -> None:
    dist, metadata = release
    verify_artifacts(dist, "0.14.0", metadata)


@pytest.mark.parametrize("location", ["build", "index-metadata", "download"])
def test_altered_distribution_is_rejected(release: tuple[Path, dict], location: str) -> None:
    dist, metadata = release
    asset = metadata["urls"][0]
    if location == "build":
        (dist / asset["filename"]).write_bytes(b"different build")
    elif location == "index-metadata":
        asset["digests"]["sha256"] = "0" * 64
    else:
        (dist.parent / "index" / asset["filename"]).write_bytes(b"different download")
    with pytest.raises(ValueError, match="differs|differ"):
        verify_artifacts(dist, "0.14.0", metadata)


@pytest.mark.parametrize("mutation", ["missing-wheel", "missing-sdist", "duplicate", "wrong-version", "yanked"])
def test_incomplete_or_unusable_release_is_rejected(release: tuple[Path, dict], mutation: str) -> None:
    dist, metadata = release
    if mutation == "missing-wheel":
        (dist / metadata["urls"][0]["filename"]).unlink()
    elif mutation == "missing-sdist":
        metadata["urls"].pop()
    elif mutation == "duplicate":
        metadata["urls"].append(metadata["urls"][0])
    elif mutation == "wrong-version":
        metadata["info"]["version"] = "0.13.0"
    else:
        metadata["urls"][0]["yanked"] = True
    with pytest.raises(ValueError):
        verify_artifacts(dist, "0.14.0", metadata)
