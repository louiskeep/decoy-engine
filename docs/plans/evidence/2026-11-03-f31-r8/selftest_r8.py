"""Seeded fixtures for the revision 8 getattr rule. Usage: python3 -I selftest_r8.py <probe.py>
`getattr(exc, "orig")` and `getattr(exc, name)` must be rows; `getattr(exc, "errno")` must not.
Red on the revision 7 probe, green on excbound_r8.py."""
import pathlib, subprocess, sys, tempfile, textwrap

ROUTE = """
    def a(f):
        try:
            return f()
        except Exception as exc:
            return {"error": getattr(exc, "orig", None)}
    def b(f, name):
        try:
            return f()
        except Exception as exc:
            return getattr(exc, name)
    def c(f):
        try:
            return f()
        except OSError as exc:
            return getattr(exc, "errno", 0)
"""
EXPECT = {"api/route.py:6": True, "api/route.py:11": True, "api/route.py:16": False}

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
