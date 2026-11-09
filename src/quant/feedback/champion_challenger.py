"""Model registry, human sign-off and champion/challenger promotion. Nothing enters production without sign-off.

Flow stage: Champion/challenger, Model registry, Human sign-off
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/ml/registry/__init__.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


class ModelRegistry:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.data: Dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {"models": {}, "champion": None}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def register(self, version: str, metrics: Dict[str, float], audit=None) -> None:
        if version in self.data["models"]:
            raise ValueError(f"version {version} already registered")
        self.data["models"][version] = {"metrics": metrics, "signed_off_by": None, "reason": None}
        self._save()
        if audit is not None:
            audit.append("model_registered", {"version": version, "metrics": metrics})

    def sign_off(self, version: str, actor: str, reason: str, audit=None) -> None:
        """A named person approves the version. Required before promotion."""
        if not actor or not reason:
            raise ValueError("sign-off needs an actor and a reason")
        self.data["models"][version]["signed_off_by"] = actor
        self.data["models"][version]["reason"] = reason
        self._save()
        if audit is not None:
            audit.append("model_signoff", {"version": version, "actor": actor, "reason": reason})

    def promote(self, version: str, audit=None) -> None:
        """Promote only if signed off and its DSR is at least the champion's."""
        m = self.data["models"][version]
        if not m["signed_off_by"]:
            raise PermissionError(f"{version} has no human sign-off")
        champ = self.data["champion"]
        if champ and m["metrics"].get("dsr", 0) < self.data["models"][champ]["metrics"].get("dsr", 0):
            raise PermissionError(f"{version} DSR below champion {champ}")
        self.data["champion"] = version
        self._save()
        if audit is not None:
            audit.append("model_promoted", {"version": version, "previous": champ})

    @property
    def champion(self) -> Optional[str]:
        return self.data["champion"]
