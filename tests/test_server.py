"""HTTP-level tests for fitsaudit.server (real sockets, in-process)."""

import http.client
import json
import threading
import unittest

from fitsaudit import fixtures
from fitsaudit.core import MAX_FILE_BYTES, audit_bytes
from fitsaudit.server import make_server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def post_fits(self, blob, content_type="application/fits"):
        return self.request("POST", "/api/fits/audit", body=blob,
                            headers={"Content-Type": content_type})

    def test_health(self):
        status, _, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "ok")

    def test_root_metadata(self):
        status, _, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["service"], "fits-audit")

    def test_audit_valid_file(self):
        status, headers, payload = self.post_fits(fixtures.build_valid_file(2))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        report = json.loads(payload)
        self.assertEqual(report["conclusion"], "ACCEPTED")
        self.assertEqual(report["hduCount"], 3)

    def test_audit_rejected_file_still_200(self):
        blob = fixtures.build_valid_file(1)[:6000]  # cut inside HDU 1 data
        status, _, payload = self.post_fits(blob)
        self.assertEqual(status, 200)
        report = json.loads(payload)
        self.assertEqual(report["conclusion"], "REJECTED")
        self.assertEqual(report["failure"]["reason"], "TRUNCATED_DATA")

    def test_empty_body_is_audited(self):
        status, _, payload = self.post_fits(b"")
        self.assertEqual(status, 200)
        report = json.loads(payload)
        self.assertEqual(report["failure"]["reason"], "EMPTY_FILE")

    def test_wrong_content_type(self):
        status, _, payload = self.post_fits(fixtures.primary_hdu(),
                                            content_type="application/json")
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(payload)["error"]["code"],
                         "UNSUPPORTED_MEDIA_TYPE")

    def test_missing_content_type(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/fits/audit")
            conn.putheader("Content-Length", "10")
            conn.endheaders(b"0123456789")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 415)
            resp.read()
        finally:
            conn.close()

    def test_oversize_declared_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/fits/audit")
            conn.putheader("Content-Type", "application/fits")
            conn.putheader("Content-Length", str(MAX_FILE_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()  # server rejects before reading body
            self.assertEqual(resp.status, 413)
            payload = json.loads(resp.read())
            self.assertEqual(payload["error"]["code"], "PAYLOAD_TOO_LARGE")
        finally:
            conn.close()

    def test_large_valid_file_below_limit(self):
        data_len = 5824 * 2880 - 2880
        cards = [fixtures.card("SIMPLE", True), fixtures.card("BITPIX", 8),
                 fixtures.card("NAXIS", 1), fixtures.card("NAXIS1", data_len)]
        blob = fixtures.header_block(cards) + fixtures.data_block(
            bytes(data_len))
        status, _, payload = self.post_fits(blob)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["conclusion"], "ACCEPTED")

    def test_unknown_path(self):
        status, _, payload = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "NOT_FOUND")

    def test_get_on_audit_path(self):
        status, headers, _ = self.request("GET", "/api/fits/audit")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_post_unknown_path(self):
        status, _, _ = self.request("POST", "/other", body=b"x",
                                    headers={"Content-Type": "application/fits"})
        self.assertEqual(status, 404)


class MaterializeServerTests(unittest.TestCase):
    """POST /api/fits/checksums/materialize over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server(host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def post_materialize(self, blob, content_type="application/fits"):
        return self.request("POST", "/api/fits/checksums/materialize",
                            body=blob,
                            headers={"Content-Type": content_type})

    def test_fills_missing_cards_and_returns_fits(self):
        blob = fixtures.build_valid_file(2, checksums=False)
        status, headers, payload = self.post_materialize(blob)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/fits")
        self.assertEqual(len(payload), len(blob))
        report = audit_bytes(payload)
        self.assertEqual(report["conclusion"], "ACCEPTED")
        for hdu in report["hdus"]:
            self.assertEqual(hdu["datasum"]["verdict"], "VALID")
            self.assertEqual(hdu["checksum"]["verdict"], "VALID")

    def test_complete_file_returned_byte_identical(self):
        blob = fixtures.build_valid_file(1)
        status, _, payload = self.post_materialize(blob)
        self.assertEqual(status, 200)
        self.assertEqual(payload, blob)

    def test_idempotent_retry(self):
        blob = fixtures.build_valid_file(1, checksums=False)
        status, _, first = self.post_materialize(blob)
        self.assertEqual(status, 200)
        status, _, second = self.post_materialize(first)
        self.assertEqual(status, 200)
        self.assertEqual(second, first)

    def test_insufficient_header_space(self):
        blob = fixtures.primary_hdu_free_slots(0)
        status, headers, payload = self.post_materialize(blob)
        self.assertEqual(status, 422)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        report = json.loads(payload)
        self.assertEqual(report["conclusion"], "REJECTED")
        failure = report["failure"]
        self.assertEqual(failure["reason"], "INSUFFICIENT_HEADER_SPACE")
        self.assertEqual(failure["hdu"], 0)
        self.assertEqual(failure["offset"], 35 * 80)
        self.assertEqual(failure["details"]["endOffset"], 35 * 80)
        self.assertEqual(failure["details"]["requiredCards"], 2)
        self.assertEqual(failure["details"]["availableCards"], 0)

    def test_corrupt_file_yields_json_not_file(self):
        blob = fixtures.corrupt_checksum(fixtures.build_valid_file(1),
                                         occurrence=1)
        status, headers, payload = self.post_materialize(blob)
        self.assertEqual(status, 422)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        report = json.loads(payload)
        self.assertEqual(report["conclusion"], "REJECTED")
        self.assertEqual(report["failure"]["reason"], "CHECKSUM_MISMATCH")

    def test_truncated_file_yields_json_not_file(self):
        blob = fixtures.build_valid_file(1)[:6000]
        status, _, payload = self.post_materialize(blob)
        self.assertEqual(status, 422)
        report = json.loads(payload)
        self.assertEqual(report["failure"]["reason"], "TRUNCATED_DATA")

    def test_wrong_content_type(self):
        status, _, payload = self.post_materialize(
            fixtures.primary_hdu(), content_type="application/json")
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(payload)["error"]["code"],
                         "UNSUPPORTED_MEDIA_TYPE")

    def test_oversize_declared_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/fits/checksums/materialize")
            conn.putheader("Content-Type", "application/fits")
            conn.putheader("Content-Length", str(MAX_FILE_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()  # server rejects before reading body
            self.assertEqual(resp.status, 413)
            payload = json.loads(resp.read())
            self.assertEqual(payload["error"]["code"], "PAYLOAD_TOO_LARGE")
        finally:
            conn.close()

    def test_get_on_materialize_path(self):
        status, headers, _ = self.request(
            "GET", "/api/fits/checksums/materialize")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_root_metadata_lists_materialize(self):
        status, _, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        endpoints = json.loads(payload)["endpoints"]
        self.assertEqual(endpoints["materialize"]["path"],
                         "/api/fits/checksums/materialize")


if __name__ == "__main__":
    unittest.main()
