"""
Regression tests for api/_nltk_bootstrap.py.

Production runs in a walled-garden environment — outbound HTTPS to
nltk.org is blocked.  The vendored ml_package calls
``nltk.download(...)`` at module import time, and the corpora it needs
must already be on disk for ``stopwords.words('english')`` and
``nltk.word_tokenize`` to succeed.

These tests assert the two invariants the bootstrap is responsible for:

  1. The bundled corpora directory is on ``nltk.data.path``.
  2. ``nltk.download`` has been replaced with a no-op so a sealed-net
     box doesn't even attempt the outbound request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

nltk = pytest.importorskip("nltk")


REFACTOR_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_NLTK  = REFACTOR_ROOT / "nltk_data"


def test_bundled_nltk_data_exists_on_disk():
    """The bundle has to actually be committed for the bootstrap to help."""
    assert BUNDLED_NLTK.is_dir(), (
        f"refactor/nltk_data/ is missing — production won't have stopwords. "
        f"expected at {BUNDLED_NLTK}"
    )
    assert (BUNDLED_NLTK / "corpora" / "stopwords" / "english").is_file()
    assert (BUNDLED_NLTK / "tokenizers" / "punkt_tab" / "english").is_dir()


def test_bootstrap_prepends_bundled_path():
    """
    Importing api triggers _nltk_bootstrap, which must put the bundled
    directory on nltk.data.path so stopwords / punkt_tab resolve there
    instead of falling through to the per-user ~/nltk_data (absent in
    prod) or a download.
    """
    import api  # noqa: F401  — side-effect import
    assert str(BUNDLED_NLTK) in nltk.data.path, (
        "bootstrap didn't add the bundled corpora to nltk.data.path; "
        "stopwords / punkt_tab will silently fall back to ~/nltk_data "
        "or a network download in production."
    )


def test_bootstrap_disables_nltk_download():
    """
    The vendored ml_package has nltk.download(...) calls at module
    scope.  In a walled-garden box the outbound HTTPS request fails
    silently and downstream stopwords.words('english') raises.  The
    bootstrap replaces nltk.download with a no-op so the upstream
    calls become safe.
    """
    import api  # noqa: F401
    # The patched function may live as an inner closure or a stub —
    # what we care about is that calling it doesn't try to phone home.
    assert nltk.download("stopwords") is True
    # Also confirm it's not the original (which would attempt HTTP):
    assert "no_op" in nltk.download.__name__.lower()


def test_english_stopwords_resolve_against_the_bundle():
    """End-to-end: the corpus must load from the bundled directory."""
    import api  # noqa: F401
    from nltk.corpus import stopwords
    words = stopwords.words("english")
    # NLTK's English stopwords list has 179-200 entries depending on
    # version; an empty list means a silent corpus failure.
    assert len(words) > 100, f"got only {len(words)} stopwords — bundle broken"
