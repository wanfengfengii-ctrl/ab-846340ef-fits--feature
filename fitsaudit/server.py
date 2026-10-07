"""HTTP API for the FITS auditor (Python standard library only).

Endpoints
---------
``GET  /health``          liveness probe used by the Docker healthcheck
``GET  /``                service metadata
``POST /api/fits/audit``  audit a FITS file (body is the raw file sent
                          as ``application/fits``, at most 16 MiB)
``POST /api/fits/checksums/materialize``
                          audit first, then fill missing DATASUM /
                          CHECKSUM cards into blank header slots after
                          the END card and return the completed file as
                          ``application/fits``

A completed audit always returns HTTP 200; the archival verdict is
carried by the ``conclusion`` field (``ACCEPTED``/``REJECTED``) of the
JSON report.  Materialization returns the completed FITS file on success
and a JSON verdict (same report shape as the audit endpoint) when no
file can be produced.  Request-level problems (wrong media type,
missing or oversized body, ...) return the matching 4xx status with a
JSON error object.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import (
    MAX_FILE_BYTES,
    MaterializationFailure,
    audit_bytes,
    materialize_checksums,
)

SERVICE_NAME = "fits-audit"
VERSION = "1.1.0"
AUDIT_PATH = "/api/fits/audit"
MATERIALIZE_PATH = "/api/fits/checksums/materialize"
FITS_MEDIA_TYPE = "application/fits"


class AuditHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "%s/%s" % (SERVICE_NAME, VERSION)
    sys_version = ""

    # -- helpers -----------------------------------------------------------

    def _send_json(self, status, payload, extra_headers=None, close=False):
        body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message, extra_headers=None, close=False):
        self._send_json(
            status,
            {"error": {"code": code, "message": message}},
            extra_headers=extra_headers,
            close=close,
        )

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        sys.stderr.write(
            "%s - %s\n" % (self.log_date_time_string(), fmt % args))

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"status": "ok", "service": SERVICE_NAME})
        elif path == "/":
            self._send_json(200, {
                "service": SERVICE_NAME,
                "version": VERSION,
                "endpoints": {
                    "audit": {"method": "POST", "path": AUDIT_PATH,
                              "contentType": FITS_MEDIA_TYPE,
                              "maxBytes": MAX_FILE_BYTES},
                    "materialize": {
                        "method": "POST", "path": MATERIALIZE_PATH,
                        "contentType": FITS_MEDIA_TYPE,
                        "maxBytes": MAX_FILE_BYTES,
                        "successContentType": FITS_MEDIA_TYPE,
                    },
                    "health": {"method": "GET", "path": "/health"},
                },
            })
        elif path == AUDIT_PATH:
            self._error(405, "METHOD_NOT_ALLOWED",
                        "use POST with Content-Type: %s" % FITS_MEDIA_TYPE,
                        extra_headers={"Allow": "POST"})
        elif path == MATERIALIZE_PATH:
            self._error(405, "METHOD_NOT_ALLOWED",
                        "use POST with Content-Type: %s" % FITS_MEDIA_TYPE,
                        extra_headers={"Allow": "POST"})
        else:
            self._error(404, "NOT_FOUND", "no such endpoint: %s" % path)

    def _read_fits_body(self):
        """Validate the framing of a FITS POST request.

        Returns the request body, or ``None`` after the matching 4xx
        error response has already been sent.  The accepted limits are
        identical for every FITS POST endpoint.
        """
        content_type = self.headers.get("Content-Type")
        media_type = (content_type.split(";", 1)[0].strip().lower()
                      if content_type else "")
        if media_type != FITS_MEDIA_TYPE:
            self._error(415, "UNSUPPORTED_MEDIA_TYPE",
                        "Content-Type must be %s" % FITS_MEDIA_TYPE,
                        close=True)
            return None

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._error(411, "LENGTH_REQUIRED",
                        "a Content-Length header is required", close=True)
            return None
        try:
            length = int(length_header)
        except ValueError:
            self._error(400, "BAD_REQUEST",
                        "invalid Content-Length: %r" % length_header,
                        close=True)
            return None
        if length < 0:
            self._error(400, "BAD_REQUEST",
                        "negative Content-Length", close=True)
            return None
        if length > MAX_FILE_BYTES:
            self._error(413, "PAYLOAD_TOO_LARGE",
                        "body exceeds the %d-byte limit" % MAX_FILE_BYTES,
                        close=True)
            return None

        body = self.rfile.read(length)
        if len(body) < length:
            self._error(400, "BAD_REQUEST",
                        "request body shorter than Content-Length",
                        close=True)
            return None
        return body

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in (AUDIT_PATH, MATERIALIZE_PATH):
            # Close the connection: the unread request body must not be
            # mistaken for the next request on a kept-alive connection.
            self._error(404, "NOT_FOUND", "no such endpoint: %s" % path,
                        close=True)
            return

        body = self._read_fits_body()
        if body is None:
            return

        if path == AUDIT_PATH:
            self._send_json(200, audit_bytes(body))
            return

        try:
            completed = materialize_checksums(body)
        except MaterializationFailure as exc:
            # The full audit rejected the input, or an HDU lacked enough
            # blank header slots: no file is produced; the JSON keeps the
            # stable audit report shape (failure.hdu/reason/offset/...).
            self._send_json(422, exc.report)
            return
        self.send_response(200)
        self.send_header("Content-Type", FITS_MEDIA_TYPE)
        self.send_header("Content-Length", str(len(completed)))
        self.end_headers()
        self.wfile.write(completed)


def make_server(host=None, port=None):
    host = host if host is not None else os.environ.get("HOST", "0.0.0.0")
    port = port if port is not None else int(os.environ.get("PORT", "8000"))
    return ThreadingHTTPServer((host, port), AuditHandler)


def main():
    httpd = make_server()
    host, port = httpd.server_address[:2]
    print("%s %s listening on %s:%d" % (SERVICE_NAME, VERSION, host, port),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
