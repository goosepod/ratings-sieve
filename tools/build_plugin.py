"""Build a local code-only archive. Does not publish, install, or restart."""

import hashlib
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
out = root / "dist" / "sonic_sieve-0.1.0.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
    for source in sorted((root / "sonic_sieve").glob("*.py")):
        archive.write(source, source.name)
print(out)
print("sha256", hashlib.sha256(out.read_bytes()).hexdigest())
