"""A reviewed code-side allowlist; uploaded metadata never grants authority."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def reviewed_sections():
    manifest = json.loads(Path(__file__).with_name("reviewed_sections.json").read_text(encoding="utf-8"))
    return {(row["section_id"], row["content_sha256"]): row for row in manifest["sections"]}
