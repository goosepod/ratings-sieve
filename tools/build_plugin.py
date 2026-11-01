"""Build a local code-only archive. Does not publish, install, or restart."""

import hashlib
from pathlib import Path
import zipfile
import tomllib

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
out = root / "dist" / f"ratings_sieve-{version}.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
    for source in sorted((root / "ratings_sieve").glob("*.py")):
        archive.write(source, source.name)
print(out)
print("sha256", hashlib.sha256(out.read_bytes()).hexdigest())
