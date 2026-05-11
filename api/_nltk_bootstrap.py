"""
NLTK bootstrap for walled-garden deployments.

The vendored ml_package calls ``nltk.download(...)`` at module import.
In production the box has no internet, so those calls fail silently and
the next ``stopwords.words('english')`` raises LookupError.

Fix: ship corpora under ``nltk_data/`` and prime NLTK before ml_package
loads — prepend the bundle to ``nltk.data.path`` and monkey-patch
``nltk.download`` to a no-op. Runs from api/__init__.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Path to the bundled corpora.  refactor/nltk_data/ — committed to git.
_BUNDLED = Path(__file__).resolve().parent.parent / "nltk_data"


def _prime_nltk() -> None:
    if not _BUNDLED.is_dir():
        # No bundle on disk — bail out and let nltk's own search path
        # handle it (developer machines with ~/nltk_data populated).
        return

    # Ensure NLTK_DATA env var is set so any subprocess inherits it.
    existing = os.environ.get("NLTK_DATA", "")
    if str(_BUNDLED) not in existing.split(os.pathsep):
        os.environ["NLTK_DATA"] = (
            f"{_BUNDLED}{os.pathsep}{existing}" if existing else str(_BUNDLED)
        )

    try:
        import nltk
    except ImportError:
        # Fast-test environment — ml_package is stubbed and nltk is
        # never imported, so there's nothing to prime.
        return

    # Prepend so the bundled copies win against any partial / corrupted
    # data in ~/nltk_data.
    if str(_BUNDLED) not in nltk.data.path:
        nltk.data.path.insert(0, str(_BUNDLED))

    # Stop ml_package's module-level download() calls from making an
    # outbound HTTPS request the deployment box can't satisfy.  Returning
    # True mirrors a successful download — the corpus is already there.
    def _no_op_download(*_args, **_kwargs):
        return True

    nltk.download = _no_op_download                     # type: ignore[assignment]
    if hasattr(nltk, "downloader"):
        nltk.downloader.download = _no_op_download      # type: ignore[attr-defined]

    # Best-effort verification: log a warning if any corpus we expect
    # the bundle to provide can't be resolved.  Non-fatal — if the file
    # is genuinely missing, the original LookupError will surface
    # downstream with a clearer call site than this bootstrap.
    for token, kind in (
        ("corpora/stopwords",          "stopwords"),
        ("tokenizers/punkt_tab/english", "punkt_tab"),
    ):
        try:
            nltk.data.find(token)
        except LookupError:
            print(
                f"[nltk-bootstrap] WARNING: bundled corpus '{kind}' not "
                f"found at {_BUNDLED / token} — runtime calls will fail.",
                file=sys.stderr,
            )


_prime_nltk()
