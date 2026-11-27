# 0041 — One OpenAlgo folder: the vendored tree (2026-11-27)

## Context
Two folders were called "OpenAlgo":

| Path | What it was | Problem |
|---|---|---|
| `src/quant/openalgo_platform/` | The vendored upstream tree, tracked by this repo, pinned in `openalgo.UPSTREAM_PIN` (decision 0013) | none — this is the real one |
| `openalgo/` | A clone made by `install.bat`, git-ignored | Never exists in a fresh checkout |

`scripts/verify/upgrade_check.py` was already fixed to default to the vendored tree
(2026-11-26). The three Windows launchers were not: they still resolved `ROOT / "openalgo"`,
so `start_openalgo.py` died with "openalgo folder not found", `start_all.py` ran docker compose
in a missing folder, and `upgrade_openalgo.ps1` did `git pull` where no repo existed.

## Decision
One folder. The vendored tree IS OpenAlgo.

- `scripts/ops/start_openalgo.py`, `scripts/ops/start_all.py`: `OPENALGO_DIR = ROOT / "src" / "quant" / "openalgo_platform"`.
- `scripts/ops/upgrade_openalgo.ps1`: `$oa = <root>\src\quant\openalgo_platform`. Upstream fetch and
  `git status`/`rev-parse` steps removed — the tree has no `.git`; its identity is the pin file.
  `-Reclone` aborts instead of re-cloning, because a clone into that path forks the code
  ("wrap, never copy"). New OpenAlgo versions arrive as a repo commit that re-vendors the tree
  plus `upgrade_check.py --update-pin`.
- `install.bat`: no `git clone`. A missing tree is a hard error telling the operator to `git pull`.
- `start_all.py --update`: no `git pull` inside the tree; it prints the pin instead.
- `src/quant/tests/test_vendored_pin.py::test_launcher_checks_the_vendored_tree` parses the
  launcher and fails if its folder ever drifts from `upgrade_check.VENDORED_REL`.

`_restored/` is unchanged and still the only home for state (`.env`, `db/`, `keys/`, `download/`),
bridged into the tree by `bootstrap_restored`.

## Consequences
- One copy of OpenAlgo. The pin check and the launcher cannot disagree.
- Local state stays out of the tracked tree: `.env` is already ignored by the vendored
  `.gitignore` (`*.env`), and `_restored/` is ignored at the repo root.
- The Ubuntu server path is aligned (2026-11-27): `Servers/install_ubuntu.sh` no longer clones
  upstream. The server's repo checkout at `/opt/my-trading-system` holds the same vendored tree at
  `src/quant/openalgo_platform`; the installer checks it, builds its `.venv` there, and bridges
  `_restored/` into it. `launch_ubuntu_server.sh` treats "installed" as the tree's `.venv` existing
  (the tree itself is always present after the repo clone). `Servers/DEPLOY_UBUNTU.md` updated.
