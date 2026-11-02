"""Seeded fixtures for excbound.py pass 2. Usage: python3 -I selftest.py <excbound.py>
Builds a throwaway api/ tree, runs the probe on it, and checks that each seeded shape is a row
and the negative controls are not. Exit 1 on any miss."""
import pathlib, subprocess, sys, tempfile, textwrap

FIXTURES = {
    "api/__init__.py": "",
    "api/lane.py": """
        class LaneDisabled(Exception):
            def __init__(self, reason):
                self.reason = reason
        def disable(registry, name, exc):
            registry[name] = str(exc)
    """,
    "api/mapper.py": """
        from api.lane import LaneDisabled
        def _map(exc):
            return Unavailable("x", f"lane down: {exc.reason}")
        def _outer(exc):
            return _map(exc)
        def admit(acquire):
            try:
                return acquire()
            except LaneDisabled as exc:
                raise _outer(exc) from exc
    """,
    "api/route.py": """
        from api.mapper import _map as mapper_alias
        def attr_read(f):
            try:
                f()
            except ValueError as err:
                return {"message": err.detail}
        def callee_import(f):
            try:
                f()
            except ValueError as err:
                return mapper_alias(err)
        def store(f, box):
            try:
                f()
            except ValueError as err:
                box.last = err
        def unresolved(f, client):
            try:
                f()
            except ValueError as err:
                client.report(err)
        def negative_code(f):
            try:
                f()
            except ValueError as err:
                return {"code": err.code}
        def negative_rewrap(f):
            try:
                f()
            except ValueError as err:
                raise RuntimeError(err.detail) from err
    """,
}

EXPECT = [  # (kind, row site, substring of the chain)
    ("callee", "api/mapper.py:4", "api/mapper.py:10"),  # _outer -> _map, depth 2 (the V1-3 shape)
    ("callee", "api/mapper.py:4", "api/route.py:11"),   # from-import alias
    ("attr", "api/route.py:7", "api/route.py:6"),       # err.detail into a returned body
    ("store", "api/route.py:17", "api/route.py:16"),
    ("unres", "api/route.py:22", "api/route.py:21"),
]
MUST_NOT = ["api/route.py:27", "api/route.py:32"]  # a closed code; a re-wrapping raise


def main():
    probe = pathlib.Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as d:
        for rel, src in FIXTURES.items():
            p = pathlib.Path(d, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src))
        out = subprocess.run([sys.executable, "-I", str(probe), d], capture_output=True, text=True).stdout
    rows = [ln.split() for ln in out.splitlines() if ln.startswith("  ")]
    got = {(r[0], r[1]) for r in rows if len(r) > 1}
    bad = 0
    for kind, site, via in EXPECT:
        ok = any(ln.split()[:2] == [kind, site] and via in ln for ln in out.splitlines())
        print(("ok   " if ok else "MISS ") + kind, site, "via", via)
        bad += not ok
    for site in MUST_NOT:
        hit = any(r[1] == site for r in rows if len(r) > 1)
        print(("FP   " if hit else "ok   ") + "negative", site)
        bad += hit
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
