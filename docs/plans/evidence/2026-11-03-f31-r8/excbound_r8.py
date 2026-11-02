"""exc-copy census, revision 8: the revision 7 allowlist probe plus getattr (Dennis M1).
Pass 2 also treats `getattr(NAME, "x"[, default])` as an attribute read: a text read unless the
name is a string literal in ALLOW. A non-literal name is always a text read. Pass 1 is unchanged.
Output is sorted, so it is byte-identical across PYTHONHASHSEED values (Dennis L5).
Usage: python3 -I excbound_r8.py <platform root>. Stdlib only; imports nothing from the target."""
import ast, pathlib

ALLOW = frozenset({"code", "errno", "status_code", "transient", "cap"})


class _AllExceptAllowed:
    """Stands in for P1_ATTRS + TEXT_ATTRS: contains every attribute name not in ALLOW."""

    def __contains__(self, attr):
        return attr not in ALLOW

    def __radd__(self, other):
        return self

    def __add__(self, other):
        return self

    def __iter__(self):
        return iter(())


INVERTED = _AllExceptAllowed()
SRC = pathlib.Path(__file__).resolve().parent.parent / "2026-11-02-f31-r6-callee" / "excbound.py"
ns = {"__name__": "excbound_r6", "__file__": str(SRC)}
exec(compile(SRC.read_text().replace('if __name__ == "__main__":', "if False:"), str(SRC), "exec"), ns)
_orig_reads_text = ns["_reads_text"]


def _reads_text(e, name, attrs=ns["P1_ATTRS"]):
    if _orig_reads_text(e, name, attrs):
        return True
    if attrs is not INVERTED:
        return False  # pass 1 keeps its revision 5 shape
    if (isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "getattr"
            and len(e.args) >= 2 and isinstance(e.args[0], ast.Name) and e.args[0].id == name):
        key = e.args[1]
        return not (isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in ALLOW)
    return False


ns["_reads_text"] = _reads_text
ns["TEXT_ATTRS"] = INVERTED
ns["main"]()
