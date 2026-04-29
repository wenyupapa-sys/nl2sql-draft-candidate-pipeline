from __future__ import annotations

from pathlib import Path


class PromptLibrary:
    def __init__(self, prompt_root: str | Path):
        self.root = Path(prompt_root)

    def load(self, name: str) -> str:
        path = self.root / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(path)
        return path.read_text(encoding="utf-8")

    def render(self, name: str, **kwargs) -> str:
        return self.load(name).format(**kwargs)
