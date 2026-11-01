"""Mutation runner for the secret-material census mechanisms (A-4 rounds 6 and 7).

Each mutant rewrites one source site, runs the tests that must catch it, and restores the file,
checking its sha256. Run it against a scratch worktree, never the tree being committed:

    python scripts/dev/secret_census_mutants.py <worktree> <python>

Pattern: the one-at-a-time, restore-and-verify mutant runner of `ooc_fk_seam_mutants.py`.
"""

import hashlib
import json
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
py = sys.argv[2]
F = "tests/fileaccess/test_secret_census_frames.py"
D = "tests/fileaccess/test_secret_census_dedicated.py"
S = "tests/sentry/test_secret_material_census.py"
OOM = "tests/unit/execution/test_ooc_fk_remap_oom_keeps_no_key.py"


def after(src, anchor, old, new):
    i = src.index(anchor)
    j = src.index(old, i)
    return src[:j] + new + src[j + len(old) :]


def drop_census(src):
    d = json.loads(src)
    n = len(d["entries"])
    d["entries"] = [
        e for e in d["entries"] if not (e["function"] == "sign_pack" and e["name"] == "key")
    ]
    if len(d["entries"]) != n - 1:
        raise SystemExit("census mutant: sign_pack.key entry not found")
    return json.dumps(d, indent=1) + "\n"


PROV = "src/decoy_engine/profiling/model_pack/provenance.py"
WIPE_KEY = "    except BaseException:\n        del key\n        raise\n"
M = [
    (
        "M1 _check_signature unboxed",
        "src/decoy_engine/profiling/model_pack/loader.py",
        lambda s: s.replace(
            "        key_box = OpaqueBox(load_signing_key_from_env())\n",
            "        signing_key = load_signing_key_from_env()\n        key_box = OpaqueBox(signing_key)\n",
        ),
        [D, F],
    ),
    (
        "M2 derive_pack_signing_key not wiped",
        PROV,
        lambda s: after(s, "def derive_pack_signing_key", "        del master\n", ""),
        [D, F],
    ),
    (
        "M3 sign_pack not wiped",
        PROV,
        lambda s: after(s, "def sign_pack", WIPE_KEY, "    except BaseException:\n        raise\n"),
        [D, F],
    ),
    (
        "M4 census entry dropped (sign_pack.key)",
        "tests/sentry/secret_material_census.json",
        drop_census,
        [S],
    ),
    (
        "M5 ShadowContext validated inside __init__",
        "src/decoy_engine/execution/physical/_shadow_context.py",
        lambda s: s.replace(
            "@validate_after_init\n@dataclass(frozen=True)\nclass ShadowContext:",
            "@dataclass(frozen=True)\nclass ShadowContext:",
        ).replace(
            "    def _validate(",
            "    def __post_init__(self) -> None:\n        self._validate()\n\n    def _validate(",
            1,
        ),
        [D, F],
    ),
    (
        "M6 closure owner not wiped (native_text_mask)",
        "src/decoy_engine/execution/native/_kernels_scalar.py",
        lambda s: after(
            s,
            "def native_text_mask",
            "    except BaseException:\n        del mask_key\n        raise\n",
            "    except BaseException:\n        raise\n",
        ),
        [D, F],
    ),
    (
        "M7 read_checked keeps chunks",
        "src/decoy_engine/fileaccess/_read.py",
        lambda s: s.replace(
            "        chunks = data = None  # type: ignore[assignment]\n        raise\n    finally:",
            "        raise\n    finally:",
        ),
        [D],
    ),
    (
        "M8 kernel wipe removed (fpe_encrypt_value), frame cell",
        "src/decoy_engine/transforms/fpe.py",
        lambda s: after(
            s, "def fpe_encrypt_value", WIPE_KEY, "    except BaseException:\n        raise\n"
        ),
        [
            F
            + "::test_census_function_keeps_no_secret_in_any_engine_frame[decoy_engine/transforms/fpe.py::fpe_encrypt_value]"
        ],
    ),
    (
        "M9 verify_manifest binds the expected MAC",
        PROV,
        lambda s: s.replace(
            "        return _hmac_mod.compare_digest(sign_manifest(manifest, key), stored)\n",
            "        expected = sign_manifest(manifest, key)\n        return _hmac_mod.compare_digest(expected, stored)\n",
        ),
        [D],
    ),
    (
        "M10 verdict checker (fpe_encrypt_value wipe removed)",
        "src/decoy_engine/transforms/fpe.py",
        lambda s: after(
            s, "def fpe_encrypt_value", WIPE_KEY, "    except BaseException:\n        raise\n"
        ),
        [S],
    ),
    (
        "M11 mint_remap_values wipe removed (round 7)",
        "src/decoy_engine/execution/out_of_core/_batch_join.py",
        lambda s: after(s, "def mint_remap_values", "        del job_seed\n", ""),
        [OOM, S],
    ),
]
for name, rel, mut, tests in M:
    p = root / rel
    src = p.read_text()
    h = hashlib.sha256(src.encode()).hexdigest()
    new = mut(src)
    if new == src:
        raise SystemExit(f"{name}: the mutation did not apply")
    p.write_text(new)
    try:
        r = subprocess.run(  # noqa: S603 - fixed pytest argv over repo-relative test paths
            [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
            cwd=root,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin", "HOME": "/root"},
        )
    finally:
        p.write_text(src)
    if hashlib.sha256(p.read_text().encode()).hexdigest() != h:
        raise SystemExit(f"restore of {rel} failed; stop and inspect the tree")
    last = r.stdout.strip().splitlines()[-1]
    print(
        f"{name} | {rel} | {'RED' if r.returncode else 'GREEN'} | {last} | restored sha256 {h[:12]}",
        flush=True,
    )
