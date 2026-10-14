"""Write the synthetic scanned invoice (image-only PDF) to the given path."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import render_invoice  # noqa: E402

target = Path(sys.argv[1])
with tempfile.TemporaryDirectory() as tmp:
    _, pdf = render_invoice(Path(tmp))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(pdf, target)
print(target)
