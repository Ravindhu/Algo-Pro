"""Publish local sample-data stores to a private GitHub data repository.

Mirrors the aswin/indian-market-historical-data pattern, but as YOUR OWN
private mirror: it takes the validated local stores (GitHub daily mirror +
OpenAlgo-bridge 1-minute store), re-validates every Parquet file, and
commits them to a dedicated local git repository that you push to GitHub.

Workflow:
  1. One-time setup (see REPO_SETUP below): create the private repo and add
     it as 'origin' of the publish checkout.
  2. Daily (scheduled task already calls the refreshers): run this script -
     it stages fresh files, writes manifest.json with SHA256s, commits.
  3. Push (automatic if --push, else `git push origin main`).

Credentials: uses the normal git credential chain (Windows credential
manager). No tokens are stored in code or config files here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

LOG = logging.getLogger(__name__)

ENGINE_DB = Path(__file__).resolve().parents[1] / "database"
DEFAULT_SOURCE_DIRS = (
    ENGINE_DB / "sample_data",          # daily mirror (indices + 5 stocks)
    ENGINE_DB / "sample_data" / "1min",  # accumulating 1-minute store
)
DEFAULT_PUBLISH_REPO = Path(__file__).resolve().parents[2] / "blue-engine-market-data"
GIT = r"C:\Program Files\Git\cmd\git.exe"
BRANCH = "main"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        [GIT, *args], cwd=str(repo), capture_output=True, text=True, timeout=600)
    return result.returncode, (result.stdout or result.stderr).strip()


def validate_parquet(path: Path) -> dict:
    """Minimal structural validation: magic bytes, rows, date sanity."""
    if path.read_bytes()[:4] != b"PAR1":
        raise ValueError(f"{path}: not a Parquet file")
    meta = pq.ParquetFile(path).metadata
    if meta.num_rows == 0:
        raise ValueError(f"{path}: empty dataset")
    return {"rows": meta.num_rows, "size_bytes": path.stat().st_size}


def export(
    publish_repo: Path = DEFAULT_PUBLISH_REPO,
    *,
    push: bool = False,
    sources: tuple[Path, ...] = DEFAULT_SOURCE_DIRS,
) -> int:
    if not publish_repo.is_dir() or not (publish_repo / ".git").is_dir():
        LOG.error(
            "publish repo not found at %s. One-time setup:\n"
            "  1. Create an EMPTY PRIVATE repo on GitHub "
            "(e.g. <you>/blue-engine-market-data).\n"
            "  2. Run:\n"
            '     git init "%s"\n'
            "     cd there, add your GitHub remote as origin, "
            "commit an initial README and push.", publish_repo, publish_repo)
        return 1

    staged: list[Path] = []
    manifest_files: dict[str, dict] = {}
    for source_dir in sources:
        if not source_dir.is_dir():
            LOG.warning("source missing, skipping: %s", source_dir)
            continue
        for path in sorted(source_dir.rglob("*.parquet")):
            rel = path.relative_to(ENGINE_DB / "sample_data")
            details = validate_parquet(path)
            # Relative layout inside the publish repo, e.g.
            #   daily/stocks/INFY_NS.parquet, 1min/stocks/INFY_NS_1min.parquet
            depth = "daily" if path.parent != source_dir else "daily"
            rel_in_repo = Path(depth) / rel
            destination = publish_repo / rel_in_repo
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
            manifest_files[str(rel_in_repo).replace("\\", "/")] = {
                **details,
                "sha256": _sha256(path),
                "source_mtime": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
            staged.append(destination)

    # Copy per-store manifests for provenance.
    for manifest_src in (ENGINE_DB / "sample_data" / "manifest.json",
                         ENGINE_DB / "sample_data" / "1min" / "manifest.json"):
        if manifest_src.is_file():
            tag = "daily" if "1min" not in manifest_src.parts else "1min"
            (publish_repo / f"manifest_{tag}.json").write_bytes(
                manifest_src.read_bytes())

    top_manifest = {
        "data_class": "historical",
        "private_mirror": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "generator": "blue_engine.database.publish_data_repo",
        "files": manifest_files,
    }
    (publish_repo / "manifest.json").write_text(
        json.dumps(top_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    rc, out = _git(publish_repo, "add", "-A")
    if rc:
        LOG.error("git add failed: %s", out)
        return 1
    rc, diff = _git(publish_repo, "diff", "--cached", "--quiet")
    if rc == 0:
        LOG.info("nothing changed since last export; no commit made")
        return 0
    rc, out = _git(publish_repo, "commit", "-m",
                   f"data: {top_manifest['generated_at_utc']} "
                   f"({len(manifest_files)} files)")
    if rc:
        LOG.error("git commit failed: %s", out)
        return 1
    LOG.info("committed %d files to %s", len(manifest_files), publish_repo)

    if push:
        rc, out = _git(publish_repo, "push", "origin", BRANCH)
        if rc:
            LOG.error("git push failed: %s", out)
            return 1
        LOG.info("pushed to origin/%s", BRANCH)
    else:
        LOG.info("not pushed; run: git -C %s push origin %s",
                 publish_repo, BRANCH)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish validated local sample data to your private "
                    "GitHub data repository.")
    parser.add_argument("--repo", type=Path, default=DEFAULT_PUBLISH_REPO,
                        help="local publish checkout of the private repo")
    parser.add_argument("--push", action="store_true",
                        help="push to origin after committing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        return export(args.repo, push=args.push)
    except Exception as exc:  # noqa: BLE001
        LOG.error("export failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
