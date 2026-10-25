"""Keep each PR's review state in a JSON file committed to a `sentinel-state` branch."""

import json
from pathlib import Path


class JsonStateStore:
    def __init__(self, root: Path):
        self.root = root

    def load(self, repo: str, pr_number: int) -> dict | None:
        path = self.root / repo.replace("/", "__") / f"{pr_number}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, repo: str, pr_number: int, state: dict) -> None:
        path = self.root / repo.replace("/", "__") / f"{pr_number}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
