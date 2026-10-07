"""Strict FITS archival auditor."""

from .core import (  # noqa: F401
    ACCEPTED,
    REJECTED,
    MAX_FILE_BYTES,
    MAX_HDUS,
    Reason,
    audit_bytes,
)

__version__ = "1.0.0"
