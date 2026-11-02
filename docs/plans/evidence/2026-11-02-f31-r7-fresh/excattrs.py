"""Every attribute name read on an except-bound name inside its handler, with counts.
Input to the revision 7 allowlist: an attribute not on the allowlist is a text read.
Usage: python3 -I excattrs.py <api dir>. Stdlib ast; imports nothing from the target."""
import ast, sys, pathlib, collections

seen = collections.Counter()
where = collections.defaultdict(list)
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts:
        continue
    t = ast.parse(p.read_text())
    for h in ast.walk(t):
        if not (isinstance(h, ast.ExceptHandler) and h.name):
            continue
        for st in h.body:
            for n in ast.walk(st):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == h.name:
                    seen[n.attr] += 1
                    if len(where[n.attr]) < 3:
                        where[n.attr].append(f"{p.relative_to(pathlib.Path(sys.argv[1]).parent)}:{n.lineno}")
for a, c in seen.most_common():
    print(f"{c:4d} {a:24s} {' '.join(where[a])}")
