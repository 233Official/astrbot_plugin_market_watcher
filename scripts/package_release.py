"""Compatibility wrapper: delegates to ``package_plugin.py``.

This script exists only for backward compatibility with CI and documentation
that reference it directly.  New code should call ``scripts/package_plugin.py``
instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from package_plugin import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
