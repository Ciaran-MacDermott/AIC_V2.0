"""
User-facing error mapping for worker failures.

The legacy pipeline raises whatever the underlying library happens to
throw — RuntimeError when a sheet is missing, KeyError when a column is
absent, FileNotFoundError when a txt file isn't where the worker
expected it.  Surfacing those tracebacks in the UI is hostile: the
analyst can't tell whether they uploaded the wrong file, named a sheet
incorrectly, or hit an actual server bug.

``classify`` maps an exception to (title, advice) so the frontend can
render a structured dialog with concrete next steps.  The technical
detail is kept separately so support can still see the traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FriendlyError:
    title:    str   # short headline shown as the dialog title
    advice:   str   # what the analyst should do to recover
    category: str = "input"   # "input" | "config" | "server"


def classify(exc: BaseException) -> FriendlyError:
    """
    Map ``exc`` to a FriendlyError describing what went wrong and how
    to fix it.  Falls back to a generic 'unexpected failure' message
    when the exception type isn't recognised.

    Order matters: more specific message-based matches go first so a
    RuntimeError saying "No FINAL sheet…" doesn't fall through to the
    generic RuntimeError branch.
    """
    msg = str(exc)
    low = msg.lower()

    # ── Phase 1 input-shape failures ─────────────────────────────────
    if "no final sheet" in low or "no meta sheet" in low:
        return FriendlyError(
            title  = "Missing required sheet",
            advice = (
                "The Excel workbook must contain both a META and a FINAL "
                "sheet. Re-export from your labelled workbook and try again."
            ),
        )
    if "meta sheet missing column" in low:
        return FriendlyError(
            title  = "META sheet is missing a required column",
            advice = (
                f"{msg}. Re-export the META sheet with the full set of "
                "expected columns (typically Attribute_Name, Attribute_Id, "
                "Lookup, ML, etc.) and re-upload."
            ),
        )

    # ── Phase 2 input-shape failures (run from extracted project dir) ─
    if isinstance(exc, FileNotFoundError):
        return FriendlyError(
            title  = "Required project file missing",
            advice = (
                f"The pipeline couldn't find {msg}. Make sure your zip "
                "contains File_For_Mapping_QC.xlsx, ModelInfo.txt, "
                "Attributes.txt and AttributeValues.txt at the same level "
                "and re-upload."
            ),
        )
    if isinstance(exc, KeyError):
        return FriendlyError(
            title  = "Expected column not found",
            advice = (
                f"A column the pipeline needed wasn't present: {msg}. "
                "Check the Phase 2 advanced config — column names like "
                "RAW_MANUFACTURER and BRAND must match your workbook headers."
            ),
            category = "config",
        )
    if isinstance(exc, ValueError) and (
        "could not convert" in low or "invalid literal" in low
    ):
        return FriendlyError(
            title  = "Bad value in input data",
            advice = (
                f"A row contained a value the pipeline couldn't parse: {msg}. "
                "Open the source xlsx, find the offending cell, and either "
                "blank it or correct the type."
            ),
        )

    # ── Phase 2 mismatch resume failures ──────────────────────────────
    if "mismatch" in low and "correction" in low:
        return FriendlyError(
            title  = "Mismatch corrections couldn't be applied",
            advice = (
                f"{msg}. Re-open the mismatch review, double-check the "
                "BRAND / TOOL_BRAND values you picked, and resume."
            ),
            category = "config",
        )

    # Default — treat as a server-side fault so the dialog asks the user
    # to pass the technical detail to support rather than try-and-retry.
    return FriendlyError(
        title    = "Pipeline failed",
        advice   = (
            "The pipeline hit an unexpected error. Download the log "
            "(below) and share it with support so we can diagnose."
        ),
        category = "server",
    )


def technical_detail(exc: BaseException, traceback_text: Optional[str]) -> str:
    """
    Format a one-line summary the dialog can show under a
    'Technical detail' disclosure.  Full traceback is kept in
    record.error for the log download / support escalation.
    """
    return f"{type(exc).__name__}: {exc}" if not traceback_text else traceback_text
