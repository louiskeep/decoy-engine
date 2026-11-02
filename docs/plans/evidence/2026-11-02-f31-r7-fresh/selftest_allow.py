"""Seeded fixtures for the revision 7 allowlist. Usage: python3 -I selftest_allow.py <probe.py>
An attribute outside the allowlist (filename, orig, strerror) must be a row; allowlisted reads
(errno, code, status_code) must not. Red on the revision 6 probe, green on excbound_allow.py."""
import pathlib, subprocess, sys, tempfile, textwrap

ROUTE = """
    def a(f):
        try:
            return f()
        except OSError as exc:
            return {"error": f"cannot open {exc.filename}"}
    def b(f):
        try:
            return f()
        except Exception as exc:
            summary = exc.orig
            return summary
    def c(f):
        try:
            return f()
        except OSError as exc:
            return exc.strerror
    def d(f):
        try:
            return f()
        except OSError as exc:
            return {"errno": exc.errno, "code": exc.code, "status": exc.status_code}
"""
EXPECT = {"api/route.py:6": True, "api/route.py:11": True, "api/route.py:17": True, "api/route.py:22": False}

with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d)
    (root / "api").mkdir()
    (root / "api" / "__init__.py").write_text("")
    (root / "api" / "route.py").write_text(textwrap.dedent(ROUTE))
    out = subprocess.run([sys.executable, "-I", sys.argv[1], str(root)], capture_output=True, text=True).stdout
    bad = 0
    for site, want in EXPECT.items():
        got = any(site in line for line in out.splitlines() if line.startswith("  "))
        print(("ok  " if got == want else "MISS"), "row" if want else "negative", site)
        bad += got != want
    sys.exit(1 if bad else 0)
