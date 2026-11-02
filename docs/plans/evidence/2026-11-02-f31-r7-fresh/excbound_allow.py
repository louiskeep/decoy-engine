"""exc-copy census, revision 7: the revision 6 probe with its text-attribute DENYLIST inverted
into a value-free ALLOWLIST (Dennis, class 8). Any attribute read on a caught exception in a copy
position is a text read unless the attribute is in ALLOW. Everything else in the revision 6 probe
(passes, callee depth 3, store shapes, unres rows) is unchanged: this wrapper loads its source and
swaps only the attribute test.
Usage: python3 -I excbound_allow.py <platform root>. Stdlib only; imports nothing from the target."""
import pathlib, sys

# Each member is value-free by the producers' types at platform 0638a97d (excattrs-0638a97d.txt):
#   code         a closed code string (the F29 P1 walk and every coded Platform error set it)
#   errno        int from OSError
#   status_code  int HTTP status
#   transient    bool (claim_source_*)
#   cap          int byte or row cap (retrieval decode child)
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


SRC = pathlib.Path(__file__).resolve().parent.parent / "2026-11-02-f31-r6-callee" / "excbound.py"
code = SRC.read_text()
ns = {"__name__": "excbound_r6", "__file__": str(SRC)}
exec(compile(code.replace('if __name__ == "__main__":', "if False:"), str(SRC), "exec"), ns)
# Pass 1 keeps its revision 5 shape (args, message) so its 56 rows stay comparable; pass 2's
# attribute test becomes "anything not in ALLOW" (`P1_ATTRS + TEXT_ATTRS` resolves to __radd__).
ns["TEXT_ATTRS"] = _AllExceptAllowed()
ns["main"]()
