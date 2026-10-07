"""Strict FITS archival auditor."""

from .core import (  # noqa: F401
    ACCEPTED,
    REJECTED,
    MAX_FILE_BYTES,
    MAX_HDUS,
    Reason,
    audit_bytes,
    materialize_checksums,
)

__version__ = "1.1.0"
