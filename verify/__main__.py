"""One-shot verification service.

Runs three verification stages and summarizes them in the process exit
code (bit flags, so failures compose):

* bit 0 (1): code tests      -- the unit test suite (unittest discover)
* bit 1 (2): application build -- byte-compilation and import of the app
* bit 2 (4): HTTP smoke      -- audit verdicts over the live API for a
  valid file, digest-corrupted files and a truncated file

Exit code 0 means every stage passed.  The service is meant to be run
once (``docker compose up --exit-code-from verify verify``) and then
exit by itself.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = os.environ.get("API_URL", "http://api:8000").rstrip("/")
HEALTH_TIMEOUT = float(os.environ.get("VERIFY_HEALTH_TIMEOUT", "60"))

EXIT_TESTS = 1
EXIT_BUILD = 2
EXIT_SMOKE = 4


def _banner(title):
    print("\n=== %s %s" % (title, "=" * (60 - len(title))), flush=True)


def run_tests():
    _banner("stage 1/3: code tests (python -m unittest)")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-t", ".", "-v"],
        cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    ok = proc.returncode == 0
    print("code tests: %s" % ("PASS" if ok else "FAIL"), flush=True)
    return ok


def run_build():
    _banner("stage 2/3: application build (byte-compile + import)")
    compile_proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "fitsaudit", "verify",
         "tests"],
        cwd=ROOT, capture_output=True, text=True)
    if compile_proc.returncode != 0:
        sys.stderr.write(compile_proc.stderr)
        print("application build: FAIL (byte-compilation)", flush=True)
        return False
    import_proc = subprocess.run(
        [sys.executable, "-c",
         "import fitsaudit.server, fitsaudit.core, fitsaudit.fixtures; "
         "print('imports ok')"],
        cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(import_proc.stdout)
    sys.stderr.write(import_proc.stderr)
    ok = import_proc.returncode == 0
    print("application build: %s" % ("PASS" if ok else "FAIL"), flush=True)
    return ok


def _post(path, blob, content_type="application/fits", raw=False):
    req = urllib.request.Request(
        API_URL + path, data=blob, method="POST",
        headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if raw:
                return resp.status, ctype, body
            return resp.status, _decode_json(body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        if raw:
            return exc.code, ctype, body
        return exc.code, _decode_json(body)


def _decode_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return {}


def _get(path):
    with urllib.request.urlopen(API_URL + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _wait_for_health():
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            status, payload = _get("/health")
            if status == 200 and payload.get("status") == "ok":
                print("API healthy at %s" % API_URL, flush=True)
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    print("API at %s did not become healthy within %.0fs"
          % (API_URL, HEALTH_TIMEOUT), flush=True)
    return False


def run_smoke():
    _banner("stage 3/3: HTTP smoke verdicts against %s" % API_URL)
    if not _wait_for_health():
        return False

    sys.path.insert(0, ROOT)
    from fitsaudit import fixtures  # noqa: PLC0415
    from fitsaudit.core import audit_bytes  # noqa: PLC0415

    valid = open(os.path.join(ROOT, "tests", "data",
                              "valid_astropy.fits"), "rb").read()
    built = fixtures.build_valid_file(2)
    corrupt_datasum = fixtures.corrupt_datasum(valid, occurrence=1)
    corrupt_checksum = fixtures.corrupt_checksum(valid, occurrence=1)
    truncated = valid[:6000]  # cut inside HDU 1's data section
    trailing = valid + b"\x00" * 100

    checks = []

    def expect(name, blob, want_status, want_conclusion=None,
               want_reason=None, want_hdu=None, want_hdus=None):
        status, report = _post("/api/fits/audit", blob)
        problems = []
        if status != want_status:
            problems.append("status %d != %d" % (status, want_status))
        if want_conclusion is not None \
                and report.get("conclusion") != want_conclusion:
            problems.append("conclusion %r != %r"
                            % (report.get("conclusion"), want_conclusion))
        failure = report.get("failure") or {}
        if want_reason is not None \
                and failure.get("reason") != want_reason:
            problems.append("reason %r != %r"
                            % (failure.get("reason"), want_reason))
        if want_hdu is not None and failure.get("hdu") != want_hdu:
            problems.append("hdu %r != %r" % (failure.get("hdu"), want_hdu))
        if want_hdus is not None and report.get("hduCount") != want_hdus:
            problems.append("hduCount %r != %r"
                            % (report.get("hduCount"), want_hdus))
        ok = not problems
        checks.append(ok)
        print("%-28s %s%s" % (
            name, "PASS" if ok else "FAIL",
            "" if ok else " (%s)" % "; ".join(problems)), flush=True)
        return report

    # A valid file (generated by astropy) must be accepted.
    report = expect("valid file accepted", valid, 200,
                    want_conclusion="ACCEPTED", want_hdus=2)
    if report.get("hdus"):
        verdicts = [(h["datasum"]["verdict"], h["checksum"]["verdict"])
                    for h in report["hdus"]]
        ok = all(v == ("VALID", "VALID") for v in verdicts)
        checks.append(ok)
        print("%-28s %s" % ("valid file checksum verdicts",
                            "PASS" if ok else "FAIL (%r)" % (verdicts,)),
              flush=True)

    # A valid file built independently (integer DATASUM form, 3 HDUs).
    expect("built valid file accepted", built, 200,
           want_conclusion="ACCEPTED", want_hdus=3)

    # Corrupted digests must be rejected with the matching reason code,
    # the earliest failing HDU and a byte offset.
    expect("corrupt DATASUM rejected", corrupt_datasum, 200,
           want_conclusion="REJECTED", want_reason="DATASUM_MISMATCH",
           want_hdu=1)
    expect("corrupt CHECKSUM rejected", corrupt_checksum, 200,
           want_conclusion="REJECTED", want_reason="CHECKSUM_MISMATCH",
           want_hdu=1)

    # A truncated file must be rejected.
    expect("truncated file rejected", truncated, 200,
           want_conclusion="REJECTED", want_reason="TRUNCATED_DATA",
           want_hdu=1)

    # Trailing bytes must be rejected as well.
    expect("trailing bytes rejected", trailing, 200,
           want_conclusion="REJECTED", want_reason="TRAILING_BYTES")

    # Wrong media type is a request-level error.
    status, _ = _post("/api/fits/audit", valid,
                      content_type="application/octet-stream")
    ok = status == 415
    checks.append(ok)
    print("%-28s %s" % ("wrong media type -> 415",
                        "PASS" if ok else "FAIL (status %d)" % status),
          flush=True)

    # -- checksum materialization smoke ---------------------------------

    materialize_path = "/api/fits/checksums/materialize"

    # Early FITS without any checksum cards: the endpoint fills the
    # missing DATASUM/CHECKSUM cards and returns a FITS body of the same
    # length (the science payload and HDU boundaries are untouched).
    early = fixtures.build_valid_file(2, checksums=False)
    status, ctype, completed = _post(materialize_path, early, raw=True)
    problems = []
    if status != 200:
        problems.append("status %d != 200" % status)
    if ctype != "application/fits":
        problems.append("content-type %r != application/fits" % ctype)
    if len(completed) != len(early):
        problems.append("length %d != %d" % (len(completed), len(early)))
    if completed == early:
        problems.append("returned bytes were not materialized")
    if problems:
        checks.append(False)
        print("%-28s FAIL (%s)" % ("missing cards materialized",
                                   "; ".join(problems)), flush=True)
    else:
        # Science sections must be byte-identical; only header padding
        # (spaces in the input) may have changed.
        early_report = audit_bytes(early)
        same_payload = all(
            completed[h["data"]["offset"]
                      :h["data"]["offset"] + h["data"]["paddedBytes"]]
            == early[h["data"]["offset"]
                     :h["data"]["offset"] + h["data"]["paddedBytes"]]
            for h in early_report["hdus"])
        only_spaces = all(a == 0x20 for a, b in zip(early, completed)
                          if a != b)
        ok = same_payload and only_spaces
        checks.append(ok)
        print("%-28s %s" % (
            "missing cards materialized",
            "PASS" if ok
            else "FAIL (payload changed: %s, non-space touched: %s)"
                 % (same_payload, only_spaces)), flush=True)

    # Post-completion re-audit: the completed file must earn a full
    # VALID/VALID verdict for every HDU through the audit endpoint.
    status, re_report = _post("/api/fits/audit", completed)
    ok = (status == 200
          and re_report.get("conclusion") == "ACCEPTED"
          and re_report.get("hduCount") == 3
          and all(h["datasum"].get("verdict") == "VALID"
                  and h["checksum"].get("verdict") == "VALID"
                  for h in re_report.get("hdus", [])))
    checks.append(ok)
    print("%-28s %s" % (
        "completed file re-audits VALID",
        "PASS" if ok else "FAIL (%r)" % re_report), flush=True)

    # Idempotent retry: materializing the result again must return the
    # exact same bytes (a fully checksummed file passes through).
    status, ctype, retried = _post(materialize_path, completed, raw=True)
    ok = status == 200 and ctype == "application/fits" \
        and retried == completed
    checks.append(ok)
    print("%-28s %s" % (
        "idempotent retry byte-identical",
        "PASS" if ok else "FAIL (status %d)" % status), flush=True)

    # An already complete input is returned byte-for-byte unchanged.
    status, _, passthrough = _post(materialize_path, valid, raw=True)
    ok = status == 200 and passthrough == valid
    checks.append(ok)
    print("%-28s %s" % ("complete input passes through",
                        "PASS" if ok else "FAIL (status %d)" % status),
          flush=True)

    # Insufficient header space: 34 content cards + END leave a single
    # blank slot while two cards are required.  The whole request fails
    # with 422 JSON naming the HDU, the reason, the END offset and the
    # slot counts -- never a (partial) FITS file.
    tight_cards = [fixtures.card("SIMPLE", True), fixtures.card("BITPIX", 8),
                   fixtures.card("NAXIS", 0)]
    tight_cards += [fixtures.card("COMMENT", None) for _ in range(31)]
    tight = fixtures.header_block(tight_cards)
    status, ctype, raw_body = _post(materialize_path, tight, raw=True)
    space_report = _decode_json(raw_body)
    failure = space_report.get("failure") or {}
    details = failure.get("details") or {}
    ok = (status == 422 and ctype == "application/json"
          and space_report.get("conclusion") == "REJECTED"
          and failure.get("hdu") == 0
          and failure.get("reason") == "INSUFFICIENT_HEADER_SPACE"
          and failure.get("offset") == 34 * 80
          and details.get("endOffset") == 34 * 80
          and details.get("availableSlots") == 1
          and details.get("requiredSlots") == 2)
    checks.append(ok)
    print("%-28s %s" % (
        "insufficient header space -> 422",
        "PASS" if ok else "FAIL (status %d, %r)" % (status, failure)),
        flush=True)

    # A structurally rejected input must not produce a file either.
    status, ctype, raw_body = _post(
        materialize_path, valid[:6000], raw=True)
    blocked = _decode_json(raw_body)
    ok = (status == 422 and ctype == "application/json"
          and (blocked.get("failure") or {}).get("reason")
          == "TRUNCATED_DATA")
    checks.append(ok)
    print("%-28s %s" % (
        "truncated input blocked -> 422",
        "PASS" if ok else "FAIL (status %d)" % status), flush=True)

    # Wrong media type is a request-level error on this endpoint too.
    status, _, _ = _post(materialize_path, early,
                         content_type="application/octet-stream", raw=True)
    ok = status == 415
    checks.append(ok)
    print("%-28s %s" % ("materialize wrong media type -> 415",
                        "PASS" if ok else "FAIL (status %d)" % status),
          flush=True)

    ok = all(checks)
    print("HTTP smoke: %s (%d/%d checks passed)"
          % ("PASS" if ok else "FAIL", sum(checks), len(checks)),
          flush=True)
    return ok


def main():
    tests_ok = run_tests()
    build_ok = run_build()
    smoke_ok = run_smoke()

    exit_code = 0
    if not tests_ok:
        exit_code |= EXIT_TESTS
    if not build_ok:
        exit_code |= EXIT_BUILD
    if not smoke_ok:
        exit_code |= EXIT_SMOKE

    _banner("verify summary")
    summary = {
        "tests": "PASS" if tests_ok else "FAIL",
        "build": "PASS" if build_ok else "FAIL",
        "smoke": "PASS" if smoke_ok else "FAIL",
        "exitCode": exit_code,
    }
    print(json.dumps(summary, indent=2), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
