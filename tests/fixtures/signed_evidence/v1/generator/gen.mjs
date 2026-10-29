// F33 signed-evidence conformance corpus generator.
//
// Independence: statements are canonicalized by the `canonicalize` npm package (the RFC 8785
// author's reference implementation), signatures are produced by node:crypto (OpenSSL) or
// panva/jose, and artifact digests by node:crypto. Nothing here shares code with the Python
// verifier under test. Expected results are written from the plan's contract
// (docs/plans/2026-10-29-f33-signed-evidence-offline-verify.md, section 1.5 and 1.9), never
// from running the Python implementation.
//
// Usage (kept out of CI; the committed corpus is frozen):
//   node gen.mjs <output-dir>
//
// TEST KEYS ONLY: every private key written under keys/ is synthetic and unrelated to any
// production key.

import {
  createHash,
  createHmac,
  generateKeyPairSync,
  sign as nodeSign,
} from "node:crypto";
import { mkdirSync, writeFileSync, rmSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import canonicalize from "canonicalize";
import { FlattenedSign, flattenedVerify, calculateJwkThumbprint, importJWK } from "jose";

const OUT = process.argv[2];
if (!OUT) throw new Error("usage: node gen.mjs <output-dir>");
rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

const sha256hex = (b) => createHash("sha256").update(b).digest("hex");
const b64u = (b) => Buffer.from(b).toString("base64url");
const canon = (o) => Buffer.from(canonicalize(o), "utf8");
const put = (rel, data) => {
  const p = join(OUT, rel);
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, data);
};
const putJson = (rel, o) => put(rel, JSON.stringify(o, null, 2) + "\n");

const TENANT = "tenant-acme";
const SIGNER = "decoy:acme-prod";
const TYP = "application/vnd.decoy.evidence-signature.v1+jws";
const CTY = "application/vnd.decoy.evidence.v1+json";
const STMT_SCHEMA = "decoy.evidence.statement/v1";
const EVAL_TIME = "2026-10-29T12:00:00Z";

// ---------------------------------------------------------------- keys
async function mkKey(curve, kid, version) {
  const { privateKey, publicKey } = generateKeyPairSync("ec", {
    namedCurve: curve === "P-256" ? "prime256v1" : "secp384r1",
  });
  const jwk = publicKey.export({ format: "jwk" });
  return { privateKey, publicKey, jwk, kid, version, curve, thumb: await calculateJwkThumbprint(jwk) };
}
const KA = await mkKey("P-256", "kv-2026-01", "2026-01");
const KB = await mkKey("P-256", "kv-2026-02", "2026-02");
const KX = await mkKey("P-256", "kv-attacker", "9999-99");
const K384 = await mkKey("P-384", "kv-p384", "2026-03");

putJson("keys/TEST-ONLY-private-keys.json", {
  warning: "SYNTHETIC TEST KEYS. Not production material. Never trust these in a real trust policy.",
  keys: Object.fromEntries(
    [KA, KB, KX, K384].map((k) => [k.kid, k.privateKey.export({ format: "jwk" })]),
  ),
});

// ------------------------------------------------------------- artifacts
function pseudo(n, seed) {
  const chunks = [];
  let h = createHash("sha256").update(seed).digest();
  while (Buffer.concat(chunks).length < n) {
    chunks.push(h);
    h = createHash("sha256").update(h).digest();
  }
  return Buffer.concat(chunks).subarray(0, n);
}
const ART = {
  out_customers: {
    path: "outputs/customers.csv",
    media: "text/csv",
    bytes: Buffer.from("id,name\n1,Alice\n2,Bob\n3,Chlo\u00e9\n", "utf8"),
    rows: 3,
    cols: 2,
  },
  out_orders: {
    path: "outputs/orders.parquet",
    media: "application/vnd.apache.parquet",
    bytes: Buffer.concat([Buffer.from("PAR1"), pseudo(2048, "orders"), Buffer.from("PAR1")]),
    rows: 12,
    cols: 4,
  },
  quarantine: {
    path: "outputs/quarantine.ndjson",
    media: "application/x-ndjson",
    bytes: Buffer.from('{"row":7,"reason":"FK_ORPHAN"}\n', "utf8"),
    rows: 1,
    cols: 2,
  },
};
for (const a of Object.values(ART)) put(join("artifacts/base", a.path), a.bytes);
const artDigest = (a) => "sha256:" + sha256hex(a.bytes);

// -------------------------------------------------------------- catalog
const REQ = (o) => ({
  media_types: null,
  min_rows_scanned: 1,
  require_full_scan: false,
  allow_empty_subject: false,
  allow_warning: false,
  optional: false,
  not_applicable_reason_codes: [],
  ...o,
});
const CATALOG = {
  schema: "decoy.required-catalog/v1",
  id: "catalog-strict",
  version: "1",
  empty_output_allowed: false,
  requirements: [
    REQ({ check_id: "leakage_sample", scope: "each_artifact", media_types: ["text/csv"], optional: true }),
    REQ({
      check_id: "residual_detection",
      scope: "each_artifact",
      media_types: ["text/csv", "application/vnd.apache.parquet"],
      require_full_scan: true,
    }),
    REQ({ check_id: "source_snapshot_binding", scope: "snapshot", min_rows_scanned: 0, allow_empty_subject: true }),
    REQ({ check_id: "structural_integrity", scope: "each_artifact" }),
  ],
};
const catDigest = (c) => "sha256:" + sha256hex(canon(c));
const catalogFiles = {};
function catalogFile(name, c) {
  putJson(`shared/${name}`, c);
  catalogFiles[name] = c;
  return `shared/${name}`;
}
const CAT_BASE = catalogFile("catalog-strict.json", CATALOG);

// ---------------------------------------------------------- trust policy
const keyEntry = (k, over = {}) => ({
  tenant: TENANT,
  signer: SIGNER,
  kid: k.kid,
  key_version: k.version,
  alg: "ES256",
  jwk_thumbprint: k.thumb,
  jwk: { kty: k.jwk.kty, crv: k.jwk.crv, x: k.jwk.x, y: k.jwk.y },
  state: "ACTIVE",
  retired_at: null,
  revoked_at: null,
  compromised_since: null,
  ...over,
});
const trustFiles = {};
function trustFile(name, keys, over = {}) {
  const obj = {
    schema: "decoy.trust-policy/v1",
    id: "tp-acme",
    version: "1",
    issued_at: "2026-10-01T00:00:00Z",
    expires_at: "2027-04-01T00:00:00Z",
    keys,
    ...over,
  };
  const bytes = Buffer.from(JSON.stringify(obj, null, 2) + "\n", "utf8");
  put(`shared/${name}`, bytes);
  trustFiles[name] = { rel: `shared/${name}`, sha: "sha256:" + sha256hex(bytes) };
  return trustFiles[name];
}
const TP_A = trustFile("trust-a-active.json", [keyEntry(KA)]);
const TP_AB = trustFile("trust-a-retired-b-active.json", [keyEntry(KA, { state: "RETIRED", retired_at: "2026-10-20T00:00:00Z" }), keyEntry(KB)]);
const TP_A_REVOKED = trustFile("trust-a-revoked.json", [keyEntry(KA, { state: "REVOKED", revoked_at: "2026-10-20T00:00:00Z" }), keyEntry(KB)]);
const TP_A_STALE = trustFile("trust-a-stale.json", [keyEntry(KA)], { expires_at: "2026-10-15T00:00:00Z" });
trustFile("trust-dup-kid.json", [keyEntry(KA), keyEntry(KB, { kid: KA.kid })]);
trustFile("trust-conflicting-version.json", [keyEntry(KA), keyEntry(KB, { key_version: KA.version, kid: "kv-other" })]);
trustFile("trust-bad-thumbprint.json", [keyEntry(KA, { jwk_thumbprint: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" })]);
trustFile("trust-alg-es384.json", [keyEntry(KA, { alg: "ES384" })]);

// ------------------------------------------------------------- statement
function mkCheck(check_id, kind, id, over = {}) {
  const a = kind === "artifact" ? ART[id] : null;
  return {
    check_id,
    subject: { kind, id },
    method: `decoy.${check_id}`,
    method_version: "1",
    status: "PASS",
    severity: "info",
    reason_code: null,
    coverage: a
      ? { rows_scanned: a.rows, rows_total: a.rows, columns: a.cols }
      : { rows_scanned: 0, rows_total: 0, columns: 0 },
    limits: { sampled: false, sample_rows: null },
    metrics: { findings: 0 },
    ...over,
  };
}
function baseChecks() {
  return [
    mkCheck("leakage_sample", "artifact", "out_customers"),
    mkCheck("residual_detection", "artifact", "out_customers"),
    mkCheck("residual_detection", "artifact", "out_orders"),
    mkCheck("source_snapshot_binding", "snapshot", "snap-001"),
    mkCheck("structural_integrity", "artifact", "out_customers"),
    mkCheck("structural_integrity", "artifact", "out_orders"),
    mkCheck("structural_integrity", "artifact", "quarantine"),
  ];
}
function baseStatement(over = {}) {
  const s = {
    schema: STMT_SCHEMA,
    bundle_id: "bundle-001",
    job_id: "job-1001",
    attempt_id: "attempt-1",
    issuer: { tenant: TENANT, signer: SIGNER },
    engine: { name: "decoy-engine", version: "0.7.0" },
    created_at: "2026-10-10T08:00:00Z",
    source: {
      snapshot_id: "snap-001",
      fingerprint: { algorithm: "sha256", version: "1", value: "sha256:" + sha256hex("source-snapshot-001") },
    },
    fingerprints: [
      { name: "legacy_manifest_hash", algorithm: "sha256", version: "r3.1", value: "sha256:" + sha256hex("legacy-manifest") },
    ],
    counts: { rows_masked: 15, rows_quarantined: 1 },
    dispositions: { masking: "COMPLETE", quarantine: "SIDECAR_INCLUDED" },
    warnings: [],
    publication: { state: "PUBLISHED", reason_code: null },
    artifacts: Object.entries(ART)
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([id, a]) => ({
        id,
        path: a.path,
        media_type: a.media,
        bytes: a.bytes.length,
        sha256: artDigest(a),
        disposition: "INCLUDED",
      })),
    policy: { id: CATALOG.id, version: CATALOG.version, digest: catDigest(CATALOG) },
    checks: baseChecks(),
  };
  return Object.assign(s, over);
}
const expectedArtifacts = () =>
  Object.entries(ART)
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([id, a]) => ({ id, sha256: artDigest(a), media_type: a.media }));

// --------------------------------------------------------------- signing
function protectedHeader(key, over = {}) {
  return {
    alg: "ES256",
    kid: key.kid,
    typ: TYP,
    cty: CTY,
    crit: ["decoy_signer", "decoy_key_version"],
    decoy_signer: SIGNER,
    decoy_key_version: key.version,
    ...over,
  };
}
// node:crypto signing over an explicit signing input; `der` keeps OpenSSL's native DER output.
function rawSign(headerBytes, payloadBytes, key, { der = false, hash = "sha256", hmac = false } = {}) {
  const input = Buffer.from(b64u(headerBytes) + "." + b64u(payloadBytes), "ascii");
  if (hmac) return createHmac("sha256", JSON.stringify(key.jwk)).update(input).digest();
  return nodeSign(hash, input, { key: key.privateKey, dsaEncoding: der ? "der" : "ieee-p1363" });
}
function sigObject(headerBytes, sigBuf) {
  return { protected: b64u(headerBytes), signature: b64u(sigBuf) };
}

const vectors = [];
function emit(id, spec) {
  const {
    statementBytes,
    statement = baseStatement(),
    sigBytes, // optional override of the whole signature.json text
    key = KA,
    header, // header object override
    headerBytes,
    sigMutate,
    der = false,
    signAs, // sign the payload that differs from the transmitted statement
    trust = TP_A,
    catalog = CAT_BASE,
    ctx = {},
    expectedPatch = {},
    expect,
    noSignature = false,
    note = "",
  } = spec;
  const stmt = statementBytes ?? canon(statement);
  const hdr = headerBytes ?? Buffer.from(JSON.stringify(header ?? protectedHeader(key)), "utf8");
  let signature = null;
  if (!noSignature) {
    if (sigBytes !== undefined) signature = Buffer.from(sigBytes);
    else {
      let sb = rawSign(hdr, signAs ?? stmt, key, { der });
      if (sigMutate) sb = sigMutate(sb);
      signature = Buffer.from(JSON.stringify(sigObject(hdr, sb)), "utf8");
    }
  }
  put(`vectors/${id}/statement.bin`, stmt);
  if (signature) put(`vectors/${id}/signature.json`, signature);
  const context = {
    purpose: "RELEASE_GATE",
    evaluation_time: EVAL_TIME,
    artifacts_root: "base",
    artifact_ops: [],
    signature_only: false,
    trust_policy_file: trust.rel,
    trust_policy_sha256: trust.sha,
    catalog_file: catalog,
    expected: {
      tenant: TENANT,
      bundle_id: "bundle-001",
      job_id: "job-1001",
      attempt_id: "attempt-1",
      snapshot_id: "snap-001",
      artifacts: expectedArtifacts(),
    },
    historical_decisions: [],
    ...ctx,
  };
  putJson(`vectors/${id}/context.json`, context);
  const exp = expect ?? {
    schema: "SUPPORTED",
    signature: "VALID",
    trust: "TRUSTED",
    artifacts: "MATCH",
    checks: "PASS",
    identity: "BOUND",
    release: "ALLOW",
    historical: "NOT_APPLICABLE",
    exit: "OK",
  };
  putJson(`vectors/${id}/expected.json`, { ...exp, ...expectedPatch, note });
  vectors.push({ id, note });
  return { stmt, hdr, signature };
}
const DENIED = (o) => ({ release: "DENY", ...o });

// ===================================================== baseline + interop
{
  // Primary valid vector: signed by panva/jose, cross-verified below.
  const stmt = canon(baseStatement());
  const jwsOut = await new FlattenedSign(stmt)
    .setProtectedHeader(protectedHeader(KA))
    .sign(KA.privateKey, { crit: { decoy_signer: true, decoy_key_version: true } });
  const detached = { protected: jwsOut.protected, signature: jwsOut.signature };
  emit("valid_release_jose", { statementBytes: stmt, sigBytes: JSON.stringify(detached), note: "signed by panva/jose FlattenedSign (independent of the Python implementation)" });
  const back = await flattenedVerify({ ...detached, payload: b64u(stmt) }, KA.publicKey, { crit: { decoy_signer: true, decoy_key_version: true } });
  if (Buffer.compare(Buffer.from(back.payload), stmt) !== 0) throw new Error("jose self-verify failed");
}
emit("valid_release_openssl", { note: "signed through node:crypto/OpenSSL P1363" });
emit("valid_release_key_b", {
  key: KB,
  trust: TP_AB,
  note: "rotation: new releases use version B while A is retained as retired",
});

// ============================================================ A1: byte flip
for (const [vid, aid] of [["a1_flip_csv", "out_customers"], ["a1_flip_parquet", "out_orders"], ["a1_flip_ndjson", "quarantine"]]) {
  emit(vid, {
    ctx: { artifact_ops: [{ op: "flip_byte", path: ART[aid].path, offset: 5 }] },
    expect: DENIED({ schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", artifacts: "MISMATCH", identity: "BOUND", exit: "TAMPERED", reasons_include: ["ARTIFACT_DIGEST_MISMATCH"] }),
    note: "one byte changed, size unchanged; signature must stay VALID",
  });
}

// ===================================================== A2: statement mutation
const tampered = { signature: "INVALID", release: "DENY", exit: "TAMPERED" };
const mutations = {
  counts: (s) => (s.counts.rows_masked = 16),
  dispositions: (s) => (s.dispositions.masking = "PARTIAL"),
  job_id: (s) => (s.job_id = "job-9999"),
  attempt_id: (s) => (s.attempt_id = "attempt-2"),
  check_status: (s) => (s.checks[1].status = "FAIL"),
  artifact_digest: (s) => (s.artifacts[0].sha256 = "sha256:" + "0".repeat(64)),
  issuer_signer: (s) => (s.issuer.signer = "decoy:other"),
  warnings: (s) => (s.warnings = ["SOMETHING_ELSE"]),
  publication: (s) => (s.publication.state = "REJECTED"),
};
for (const [name, fn] of Object.entries(mutations)) {
  const signedStmt = canon(baseStatement());
  const m = baseStatement();
  fn(m);
  emit(`a2_mut_${name}`, { statementBytes: canon(m), signAs: signedStmt, expect: tampered, note: "statement mutated after signing, re-canonicalized" });
}
// header-field mutation: protected bytes replaced, signature left alone.
for (const [name, over] of Object.entries({ decoy_signer: { decoy_signer: "decoy:other" }, decoy_key_version: { decoy_key_version: "2026-02" }, kid: { kid: "kv-2026-02" } })) {
  const good = Buffer.from(JSON.stringify(protectedHeader(KA)), "utf8");
  const stmt = canon(baseStatement());
  const sb = rawSign(good, stmt, KA);
  const swapped = Buffer.from(JSON.stringify(protectedHeader(KA, over)), "utf8");
  emit(`a2_mut_header_${name}`, { statementBytes: stmt, sigBytes: JSON.stringify(sigObject(swapped, sb)), trust: TP_AB, expect: tampered, note: "protected header swapped after signing" });
}

// ========================================= canonicalization / strict parsing
const sorted = baseStatement();
const pretty = Buffer.from(JSON.stringify(sorted, null, 2), "utf8");
const nc = { schema: "NON_CANONICAL", signature: "VALID", release: "DENY", exit: "MALFORMED", reasons_include: ["STATEMENT_NOT_CANONICAL"] };
emit("c_noncanonical_whitespace", { statementBytes: pretty, expect: nc, note: "signed over pretty-printed bytes: authentic but not canonical" });
emit("c_noncanonical_key_order", { statementBytes: Buffer.from(JSON.stringify(Object.fromEntries(Object.entries(sorted).reverse())), "utf8"), expect: nc });
emit("c_noncanonical_trailing_newline", { statementBytes: Buffer.concat([canon(sorted), Buffer.from("\n")]), expect: nc });
emit("c_noncanonical_unicode_escape", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace("decoy-engine", "decoy\\u002dengine"), "utf8"), expect: nc, note: "valid JSON escape of an ASCII char is not the JCS form" });
const malformed = (code) => ({ schema: "MALFORMED", release: "DENY", exit: "MALFORMED", reasons_include: [code] });
emit("c_bom", { statementBytes: Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), canon(sorted)]), expect: malformed("STATEMENT_BOM") });
emit("c_invalid_utf8", { statementBytes: Buffer.concat([canon(sorted).subarray(0, 20), Buffer.from([0xff, 0xfe]), canon(sorted).subarray(20)]), expect: malformed("STATEMENT_NOT_UTF8") });
emit("c_trailing_garbage", { statementBytes: Buffer.concat([canon(sorted), Buffer.from("x")]), expect: malformed("STATEMENT_NOT_JSON") });
emit("c_duplicate_key_top", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace('"job_id":"job-1001"', '"job_id":"job-1001","job_id":"job-1002"'), "utf8"), expect: malformed("STATEMENT_DUPLICATE_MEMBER") });
emit("c_duplicate_key_nested", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace('"tenant":"tenant-acme"', '"tenant":"tenant-acme","tenant":"tenant-evil"'), "utf8"), expect: malformed("STATEMENT_DUPLICATE_MEMBER") });
emit("c_float_number", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace('"rows_masked":15', '"rows_masked":15.5'), "utf8"), expect: malformed("STATEMENT_FLOAT_NOT_ALLOWED") });
emit("c_nan", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace('"rows_masked":15', '"rows_masked":NaN'), "utf8"), expect: malformed("STATEMENT_NON_FINITE_NUMBER") });
emit("c_unsafe_integer", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace('"rows_masked":15', '"rows_masked":9007199254740993'), "utf8"), expect: malformed("STATEMENT_INTEGER_OUT_OF_DOMAIN") });
emit("c_negative_zero", { statementBytes: Buffer.from(canon(sorted).toString("utf8").replace('"rows_masked":15', '"rows_masked":-0'), "utf8"), expect: malformed("STATEMENT_INTEGER_OUT_OF_DOMAIN") });
{
  const big = baseStatement();
  big.counts.rows_masked = "9007199254740993";
  emit("c_decimal_string_large_count", { statement: big, note: "counts beyond 2^53-1 are decimal strings" });
  const small = baseStatement();
  small.counts.rows_masked = "15";
  emit("c_decimal_string_small_count", { statement: small, expect: malformed("STATEMENT_SCHEMA_INVALID"), note: "a count that fits as an integer MUST be an integer: one representation per value" });
}
emit("c_lone_surrogate_path", {
  statementBytes: Buffer.from(canon(sorted).toString("utf8").replace("outputs/customers.csv", "outputs/bad\\ud800.csv"), "utf8"),
  expect: malformed("STATEMENT_INVALID_UNICODE"),
});
emit("c_unknown_field", { statement: baseStatement({ extra: 1 }), expect: malformed("STATEMENT_UNKNOWN_FIELD") });
emit("c_unknown_schema", { statement: baseStatement({ schema: "decoy.evidence.statement/v2" }), expect: { schema: "UNSUPPORTED", signature: "VALID", release: "DENY", exit: "UNSUPPORTED", reasons_include: ["STATEMENT_SCHEMA_UNSUPPORTED"] } });
emit("c_deep_nesting", { statementBytes: Buffer.from("{" + '"a":' + "[".repeat(200) + "]".repeat(200) + "}", "utf8"), expect: malformed("STATEMENT_TOO_DEEP") });
emit("c_legacy_r31_unsigned", {
  statementBytes: Buffer.from(JSON.stringify({ schema_version: "r3.1", job_id: "job-1001", manifest_hash: "sha256:" + sha256hex("x"), signature: null, signature_alg: null, signature_key_id: null }), "utf8"),
  noSignature: true,
  expect: { schema: "UNSUPPORTED", signature: "UNSIGNED", release: "DENY", exit: "UNSUPPORTED", reasons_include: ["LEGACY_INTEGRITY_ONLY"] },
  note: "legacy r3.1 manifest: integrity-only, never a signed pass",
});
emit("c_unsigned_f33", { noSignature: true, expect: { schema: "SUPPORTED", signature: "UNSIGNED", release: "DENY", exit: "UNSUPPORTED", reasons_include: ["SIGNATURE_MISSING"] } });
emit("c_unicode_paths_ok", {
  statement: (() => {
    const s = baseStatement();
    const a = s.artifacts.find((x) => x.id === "out_customers");
    a.path = "r\u00e9sum\u00e9/\ud83d\ude00-data.csv";
    return s;
  })(),
  ctx: { artifact_ops: [{ op: "rename", from: ART.out_customers.path, to: "r\u00e9sum\u00e9/\ud83d\ude00-data.csv" }] },
  note: "astral and non-ASCII path preserved without normalization",
});

// ---------------------------------------------------- path / inventory
const bad = (path) => {
  const s = baseStatement();
  s.artifacts[0].path = path;
  return s;
};
for (const [name, p] of Object.entries({ dotdot: "../secret.csv", absolute: "/etc/passwd", backslash: "outputs\\customers.csv", dot_component: "outputs/./customers.csv", empty_component: "outputs//customers.csv", control_char: "outputs/cu\u0001stomers.csv", trailing_slash: "outputs/", drive: "C:/x.csv" })) {
  emit(`f_path_${name}`, { statement: bad(p), expect: malformed("STATEMENT_PATH_INVALID") });
}
{
  const s = baseStatement();
  s.artifacts[1].path = s.artifacts[0].path;
  emit("f_duplicate_paths", { statement: s, expect: malformed("STATEMENT_PATH_DUPLICATE") });
  const d = baseStatement();
  d.artifacts.push({ ...d.artifacts[0] });
  emit("f_duplicate_artifact_id", { statement: d, expect: malformed("STATEMENT_ARTIFACT_ID_DUPLICATE") });
  const u = baseStatement();
  [u.artifacts[0], u.artifacts[1]] = [u.artifacts[1], u.artifacts[0]];
  emit("f_unsorted_artifacts", { statement: u, expect: malformed("STATEMENT_ARRAY_ORDER") });
}
const incomplete = { schema: "SUPPORTED", signature: "VALID", release: "DENY", exit: "INCOMPLETE" };
emit("f_missing_file", { ctx: { artifact_ops: [{ op: "delete", path: ART.out_orders.path }] }, expect: { ...incomplete, artifacts: "MISSING", reasons_include: ["ARTIFACT_MISSING"] } });
emit("f_extra_unbound_file", { ctx: { artifact_ops: [{ op: "add", path: "outputs/extra.csv", content_b64: b64u("a,b\n1,2\n") }] }, expect: { ...incomplete, artifacts: "UNEXPECTED", reasons_include: ["ARTIFACT_UNBOUND_FILE"] } });
emit("f_signature_only", { ctx: { signature_only: true }, expect: { schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", artifacts: "NOT_EVALUATED", release: "DENY", exit: "PARTIAL", reasons_include: ["ARTIFACTS_NOT_EVALUATED"] }, note: "diagnostic mode never returns full success" });
emit("f_expected_digest_differs", {
  ctx: { expected: { tenant: TENANT, bundle_id: "bundle-001", job_id: "job-1001", attempt_id: "attempt-1", snapshot_id: "snap-001", artifacts: expectedArtifacts().map((a, i) => (i === 0 ? { ...a, sha256: "sha256:" + "1".repeat(64) } : a)) } },
  expect: DENIED({ artifacts: "MISMATCH", exit: "TAMPERED", reasons_include: ["EXPECTED_DIGEST_MISMATCH"] }),
  note: "the signed digest disagrees with the digest the trusted caller expects",
});
emit("f_expected_artifact_missing_from_statement", {
  statement: (() => {
    const s = baseStatement();
    s.artifacts = s.artifacts.filter((a) => a.id !== "quarantine");
    s.checks = s.checks.filter((c) => c.subject.id !== "quarantine");
    return s;
  })(),
  expect: DENIED({ artifacts: "MISSING", exit: "INCOMPLETE", reasons_include: ["ARTIFACT_NOT_IN_STATEMENT"] }),
  note: "producer omitted an artifact the caller expects",
});
emit("f_statement_has_unexpected_artifact", {
  statement: (() => {
    const s = baseStatement();
    s.artifacts.push({ id: "zz_extra", path: "outputs/zz.csv", media_type: "text/csv", bytes: 3, sha256: "sha256:" + sha256hex("zz\n"), disposition: "INCLUDED" });
    return s;
  })(),
  expect: DENIED({ artifacts: "UNEXPECTED", exit: "INCOMPLETE", reasons_include: ["ARTIFACT_NOT_EXPECTED"] }),
});

// ------------------------------------------------ envelope / profile (P1363 etc.)
const envMalformed = (code) => ({ signature: "INVALID", release: "DENY", exit: "MALFORMED", reasons_include: [code] });
const stmtOk = canon(baseStatement());
const goodHdr = (o) => Buffer.from(JSON.stringify(protectedHeader(KA, o)), "utf8");
emit("p_alg_none", { header: protectedHeader(KA, { alg: "none" }), sigBytes: JSON.stringify({ protected: b64u(goodHdr({ alg: "none" })), signature: "" }), expect: envMalformed("ENVELOPE_ALG_NOT_ES256") });
emit("p_alg_hs256", { headerBytes: goodHdr({ alg: "HS256" }), sigMutate: undefined, sigBytes: JSON.stringify(sigObject(goodHdr({ alg: "HS256" }), rawSign(goodHdr({ alg: "HS256" }), stmtOk, KA, { hmac: true }))), expect: envMalformed("ENVELOPE_ALG_NOT_ES256"), note: "classic key-confusion: HMAC keyed with the public key material" });
emit("p_alg_es384_p384", { key: K384, header: protectedHeader(K384, { alg: "ES384" }), sigBytes: JSON.stringify(sigObject(Buffer.from(JSON.stringify(protectedHeader(K384, { alg: "ES384" })), "utf8"), rawSign(Buffer.from(JSON.stringify(protectedHeader(K384, { alg: "ES384" })), "utf8"), stmtOk, K384, { hash: "sha384" }))), expect: envMalformed("ENVELOPE_ALG_NOT_ES256") });
emit("p_alg_unknown", { headerBytes: goodHdr({ alg: "XX999" }), sigBytes: JSON.stringify(sigObject(goodHdr({ alg: "XX999" }), Buffer.alloc(64, 1))), expect: envMalformed("ENVELOPE_ALG_NOT_ES256") });
emit("p_sig_der", { der: true, expect: envMalformed("SIGNATURE_ENCODING_INVALID"), note: "valid ECDSA signature, DER instead of fixed-width P1363" });
emit("p_sig_truncated", { sigMutate: (s) => s.subarray(0, 63), expect: envMalformed("SIGNATURE_ENCODING_INVALID") });
emit("p_sig_overlong", { sigMutate: (s) => Buffer.concat([s, Buffer.from([0])]), expect: envMalformed("SIGNATURE_ENCODING_INVALID") });
emit("p_sig_empty", { sigMutate: () => Buffer.alloc(0), expect: envMalformed("SIGNATURE_ENCODING_INVALID") });
emit("p_sig_padded_base64", { sigBytes: JSON.stringify({ protected: b64u(goodHdr({})), signature: Buffer.from(rawSign(goodHdr({}), stmtOk, KA)).toString("base64") }), expect: envMalformed("ENVELOPE_BASE64URL_INVALID"), note: "standard base64 with padding/alphabet is not base64url" });
emit("p_sig_bitflip", { sigMutate: (s) => { const c = Buffer.from(s); c[10] ^= 0x01; return c; }, expect: tampered });
emit("p_sig_high_s_accepted", {
  // A valid (r, n-s) twin. Low-s canonicalisation is explicitly OUT of scope (plan 2.3): standard JOSE verify accepts both.
  sigMutate: (s) => {
    const n = BigInt("0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551");
    const r = s.subarray(0, 32);
    const sv = BigInt("0x" + s.subarray(32).toString("hex"));
    const twin = n - sv;
    return Buffer.concat([r, Buffer.from(twin.toString(16).padStart(64, "0"), "hex")]);
  },
  note: "(r, n-s) is mathematically valid; both forms verify. Documents that low-s is not enforced.",
});
emit("p_crit_unsupported", { headerBytes: goodHdr({ crit: ["decoy_signer", "decoy_key_version", "x-unknown"], "x-unknown": 1 }), expect: envMalformed("ENVELOPE_CRIT_INVALID") });
{
  const h = protectedHeader(KA);
  delete h.crit;
  emit("p_crit_missing", { header: h, expect: envMalformed("ENVELOPE_CRIT_INVALID") });
  const h2 = protectedHeader(KA);
  delete h2.decoy_signer;
  emit("p_missing_signer", { header: h2, expect: envMalformed("ENVELOPE_HEADER_FIELD_MISSING") });
  const h3 = protectedHeader(KA);
  delete h3.kid;
  emit("p_missing_kid", { header: h3, expect: envMalformed("ENVELOPE_HEADER_FIELD_MISSING") });
}
emit("p_typ_wrong", { header: protectedHeader(KA, { typ: "JWT" }), expect: envMalformed("ENVELOPE_TYP_INVALID") });
emit("p_cty_wrong", { header: protectedHeader(KA, { cty: "application/json" }), expect: envMalformed("ENVELOPE_CTY_INVALID") });
emit("p_b64_false", { headerBytes: goodHdr({ b64: false, crit: ["decoy_signer", "decoy_key_version", "b64"] }), expect: envMalformed("ENVELOPE_HEADER_MEMBER_FORBIDDEN") });
emit("p_embedded_jwk", { headerBytes: Buffer.from(JSON.stringify({ ...protectedHeader(KX), kid: KA.kid, decoy_key_version: KA.version, jwk: { kty: KX.jwk.kty, crv: KX.jwk.crv, x: KX.jwk.x, y: KX.jwk.y } }), "utf8"), key: KX, expect: envMalformed("ENVELOPE_HEADER_MEMBER_FORBIDDEN"), note: "attacker key embedded in the header and used to sign" });
emit("p_jku", { header: protectedHeader(KA, { jku: "https://attacker.example/keys.json" }), expect: envMalformed("ENVELOPE_HEADER_MEMBER_FORBIDDEN") });
{
  const dup = Buffer.from(JSON.stringify(protectedHeader(KA)).replace('"alg":"ES256"', '"alg":"ES256","alg":"ES256"'), "utf8");
  emit("p_duplicate_header_member", { headerBytes: dup, expect: envMalformed("ENVELOPE_DUPLICATE_MEMBER"), note: "duplicate member that a last-wins parser would hide" });
  const hdr = goodHdr({});
  const sb = rawSign(hdr, stmtOk, KA);
  const o = sigObject(hdr, sb);
  emit("p_unprotected_header", { sigBytes: JSON.stringify({ ...o, header: { kid: KA.kid } }), expect: envMalformed("ENVELOPE_UNPROTECTED_HEADER") });
  emit("p_payload_attached", { sigBytes: JSON.stringify({ ...o, payload: b64u(stmtOk) }), expect: envMalformed("ENVELOPE_PAYLOAD_PRESENT") });
  emit("p_general_multi_signature", { sigBytes: JSON.stringify({ signatures: [o, o] }), expect: envMalformed("ENVELOPE_SHAPE_UNSUPPORTED") });
  emit("p_extra_member", { sigBytes: JSON.stringify({ ...o, extra: 1 }), expect: envMalformed("ENVELOPE_SHAPE_UNSUPPORTED") });
  emit("p_envelope_not_json", { sigBytes: "not json", expect: envMalformed("ENVELOPE_NOT_JSON") });
  emit("p_protected_not_json", { sigBytes: JSON.stringify({ protected: b64u("not json"), signature: o.signature }), expect: envMalformed("ENVELOPE_PROTECTED_NOT_JSON") });
}
emit("p_wrong_signing_key", { key: KX, header: protectedHeader(KX, { kid: KA.kid, decoy_key_version: KA.version }), expect: tampered, note: "kid/version claim key A, signature is by an attacker key: signature must not verify" });
emit("p_unknown_kid", { header: protectedHeader(KA, { kid: "kv-unknown" }), expect: { signature: "NOT_EVALUATED", trust: "UNKNOWN_KEY", release: "DENY", exit: "UNTRUSTED", reasons_include: ["KEY_UNKNOWN"] } });
emit("p_kid_version_mismatch", { header: protectedHeader(KA, { decoy_key_version: "2026-02" }), expect: { signature: "VALID", trust: "SCOPE_MISMATCH", release: "DENY", exit: "UNTRUSTED", reasons_include: ["KEY_VERSION_MISMATCH"] } });
emit("p_signer_header_vs_statement", { header: protectedHeader(KA, { decoy_signer: "decoy:other" }), trust: trustFile("trust-signer-other.json", [keyEntry(KA, { signer: "decoy:other" })]), expect: { signature: "VALID", trust: "SCOPE_MISMATCH", release: "DENY", exit: "UNTRUSTED", reasons_include: ["SIGNER_MISMATCH"] }, note: "header signer matches the key but not the signed issuer.signer" });

// ------------------------------------------------------- trust / lifecycle
const untrusted = (trust, hist, reason) => ({ signature: "VALID", trust, historical: hist, release: "DENY", exit: "UNTRUSTED", reasons_include: [reason] });
emit("t_retired_consumer_accepts", { trust: TP_AB, ctx: { purpose: "CONSUMER_ACCEPTANCE" }, expect: { schema: "SUPPORTED", signature: "VALID", trust: "RETIRED", artifacts: "MATCH", checks: "PASS", identity: "BOUND", release: "ALLOW", historical: "NOT_APPLICABLE", exit: "OK", reasons_include: ["SIGNER_RETIRED"] }, note: "routine rotation: old bundle still verifies, reported as retired" });
emit("t_retired_release_gate_denies", { trust: TP_AB, expect: untrusted("RETIRED", "NOT_APPLICABLE", "SIGNER_NOT_ACTIVE") });
emit("t_revoked_created_before_revocation", { trust: TP_A_REVOKED, ctx: { purpose: "CONSUMER_ACCEPTANCE" }, expect: untrusted("REVOKED", "UNKNOWN", "SIGNER_REVOKED"), note: "created_at (2026-10-10) predates revocation but is signer-asserted: history stays UNKNOWN" });
emit("t_revoked_created_after_revocation", { statement: baseStatement({ created_at: "2026-10-25T08:00:00Z" }), trust: TP_A_REVOKED, ctx: { purpose: "CONSUMER_ACCEPTANCE" }, expect: untrusted("REVOKED", "DISALLOWED", "SIGNER_REVOKED") });
{
  const st = canon(baseStatement());
  const hdr = goodHdr({});
  const sig = JSON.stringify(sigObject(hdr, rawSign(hdr, st, KA)));
  const decision = { statement_sha256: "sha256:" + sha256hex(st), signature_sha256: "sha256:" + sha256hex(Buffer.from(sig)), decision_ref: "archive://decision-2026-10-12", policy_digest: "sha256:" + sha256hex("historical-policy") };
  emit("t_revoked_archived_decision_audit", { statementBytes: st, sigBytes: sig, trust: TP_A_REVOKED, ctx: { purpose: "HISTORICAL_AUDIT", historical_decisions: [decision] }, expect: { signature: "VALID", trust: "REVOKED", historical: "ESTABLISHED", release: "DENY", exit: "PARTIAL", reasons_include: ["SIGNER_REVOKED", "HISTORICAL_DECISION_ESTABLISHED"] }, note: "an authenticated archived decision binds this exact statement+signature; current revocation still reported" });
  emit("t_revoked_archived_decision_acceptance", { statementBytes: st, sigBytes: sig, trust: TP_A_REVOKED, ctx: { purpose: "CONSUMER_ACCEPTANCE", historical_decisions: [decision] }, expect: { signature: "VALID", trust: "REVOKED", historical: "ESTABLISHED", release: "DENY", exit: "UNTRUSTED" }, note: "historical evidence never converts into current acceptance" });
  const wrong = { ...decision, statement_sha256: "sha256:" + "2".repeat(64) };
  emit("t_revoked_archived_decision_other_statement", { statementBytes: st, sigBytes: sig, trust: TP_A_REVOKED, ctx: { purpose: "HISTORICAL_AUDIT", historical_decisions: [wrong] }, expect: { trust: "REVOKED", historical: "UNKNOWN", exit: "PARTIAL" } });
}
emit("t_stale_policy", { trust: TP_A_STALE, expect: untrusted("STALE_POLICY", "UNKNOWN", "TRUST_POLICY_STALE"), note: "valid signature; expired trust snapshot grants no current clearance" });
emit("t_historical_audit_never_ok", { ctx: { purpose: "HISTORICAL_AUDIT" }, expect: { schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", artifacts: "MATCH", checks: "PASS", release: "DENY", exit: "PARTIAL", reasons_include: ["PURPOSE_HISTORICAL_AUDIT"] }, note: "a clean historical audit is never presented as current clearance" });
emit("t_scope_mismatch_tenant", { ctx: { expected: { tenant: "tenant-other", bundle_id: "bundle-001", job_id: "job-1001", attempt_id: "attempt-1", snapshot_id: "snap-001", artifacts: expectedArtifacts() } }, expect: untrusted("SCOPE_MISMATCH", "NOT_APPLICABLE", "TENANT_MISMATCH") });
for (const [name, file, code] of [["dup_kid", "trust-dup-kid.json", "TRUST_POLICY_DUPLICATE_KID"], ["conflicting_version", "trust-conflicting-version.json", "TRUST_POLICY_KEY_CONFLICT"], ["bad_thumbprint", "trust-bad-thumbprint.json", "TRUST_POLICY_THUMBPRINT_MISMATCH"], ["alg_es384", "trust-alg-es384.json", "TRUST_POLICY_ALG_UNSUPPORTED"]]) {
  emit(`t_policy_${name}`, { trust: trustFiles[file], expect: { load_error: code } });
}
emit("t_policy_digest_pin_mismatch", { ctx: { trust_policy_sha256: "sha256:" + "3".repeat(64) }, expect: { load_error: "TRUST_POLICY_DIGEST_MISMATCH" } });

// ----------------------------------------------------------------- identity
const blocked = (id, code) => ({ schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", identity: "MISMATCH", release: "DENY", exit: "BLOCKED", reasons_include: [code] });
const exp0 = { tenant: TENANT, bundle_id: "bundle-001", job_id: "job-1001", attempt_id: "attempt-1", snapshot_id: "snap-001", artifacts: expectedArtifacts() };
emit("i_replay_other_attempt", { ctx: { expected: { ...exp0, attempt_id: "attempt-2" } }, expect: blocked("attempt", "IDENTITY_ATTEMPT_MISMATCH"), note: "authentic statement for a different attempt is not evidence for this release" });
emit("i_replay_other_job", { ctx: { expected: { ...exp0, job_id: "job-2002" } }, expect: blocked("job", "IDENTITY_JOB_MISMATCH") });
emit("i_replay_other_bundle", { ctx: { expected: { ...exp0, bundle_id: "bundle-002" } }, expect: blocked("bundle", "IDENTITY_BUNDLE_MISMATCH") });
emit("i_replay_other_snapshot", { ctx: { expected: { ...exp0, snapshot_id: "snap-002" } }, expect: blocked("snapshot", "IDENTITY_SNAPSHOT_MISMATCH") });
emit("i_historical_no_expectations", { ctx: { purpose: "HISTORICAL_AUDIT", expected: { artifacts: expectedArtifacts() } }, expect: { schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", identity: "NOT_ASSERTED", release: "DENY", exit: "PARTIAL" } });
emit("i_release_context_incomplete", { ctx: { expected: { tenant: TENANT, artifacts: expectedArtifacts() } }, expect: { load_error: "CONTEXT_INCOMPLETE" } });

// --------------------------------------------------- checks / catalog (A3, A4)
const blockedChecks = (checks, code, extra = {}) => ({ schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", artifacts: "MATCH", checks, release: "DENY", exit: "BLOCKED", reasons_include: [code], ...extra });
const withChecks = (fn, over = {}) => {
  const s = baseStatement(over);
  s.checks = fn(s.checks);
  return s;
};
const setStatus = (checks, cid, aid, patch) => checks.map((c) => (c.check_id === cid && c.subject.id === aid ? { ...c, ...patch } : c));
emit("k_required_inconclusive_published", {
  statement: withChecks((c) => setStatus(c, "residual_detection", "out_orders", { status: "INCONCLUSIVE", reason_code: "DETECTOR_UNAVAILABLE" })),
  expect: blockedChecks("INCONCLUSIVE", "CHECK_INCONCLUSIVE"),
  note: "signature valid, state PUBLISHED, yet a required check is inconclusive: no release",
});
emit("k_required_inconclusive_signed_rejection", {
  statement: (() => {
    const s = withChecks((c) => setStatus(c, "residual_detection", "out_orders", { status: "INCONCLUSIVE", reason_code: "DETECTOR_UNAVAILABLE" }), { publication: { state: "REJECTED", reason_code: "REQUIRED_CHECK_INCONCLUSIVE" } });
    s.artifacts = s.artifacts.map((a) => ({ ...a, disposition: "WITHHELD" }));
    return s;
  })(),
  ctx: { artifact_ops: [{ op: "delete_all" }] },
  expect: blockedChecks("INCONCLUSIVE", "CHECK_INCONCLUSIVE", { artifacts: "WITHHELD", reasons_include: ["CHECK_INCONCLUSIVE", "PUBLICATION_STATE_REJECTED"] }),
  note: "a signed rejection is exportable for audit and never authorizes a release",
});
emit("k_optional_inconclusive_control", {
  statement: withChecks((c) => setStatus(c, "leakage_sample", "out_customers", { status: "INCONCLUSIVE", reason_code: "SAMPLE_UNAVAILABLE" })),
  expect: { schema: "SUPPORTED", signature: "VALID", trust: "TRUSTED", artifacts: "MATCH", checks: "PASS", release: "ALLOW", exit: "OK", notes_include: ["OPTIONAL_CHECK_INCONCLUSIVE"] },
  note: "optional inconclusive stays visible without blocking",
});
emit("k_required_fail", { statement: withChecks((c) => setStatus(c, "residual_detection", "out_customers", { status: "FAIL", severity: "error", reason_code: "RESIDUAL_FINDINGS" })), expect: blockedChecks("FAIL", "CHECK_FAILED") });
emit("k_missing_check", { statement: withChecks((c) => c.filter((x) => !(x.check_id === "structural_integrity" && x.subject.id === "quarantine"))), expect: blockedChecks("INCONCLUSIVE", "CHECK_SUBJECT_MISSING") });
emit("k_missing_residual_detector", { statement: withChecks((c) => c.filter((x) => x.check_id !== "residual_detection")), expect: blockedChecks("INCONCLUSIVE", "CHECK_SUBJECT_MISSING"), note: "empty findings / absent detector never proves completion" });
emit("k_all_checks_absent", { statement: withChecks(() => []), expect: blockedChecks("INCONCLUSIVE", "CHECK_SUBJECT_MISSING"), note: "validation off / skip requested" });
emit("k_duplicate_conflicting", { statement: withChecks((c) => [...c, mkCheck("residual_detection", "artifact", "out_customers", { status: "FAIL", severity: "error", reason_code: "RESIDUAL_FINDINGS" })].sort((a, b) => (a.check_id + a.subject.kind + a.subject.id < b.check_id + b.subject.kind + b.subject.id ? -1 : 1))), expect: blockedChecks("INCONCLUSIVE", "CHECK_SUBJECT_DUPLICATE") });
emit("k_unknown_check", { statement: withChecks((c) => [...c, mkCheck("zz_mystery", "artifact", "out_customers")]), expect: blockedChecks("INCONCLUSIVE", "CHECK_NOT_IN_CATALOG") });
emit("k_zero_row_pass", { statement: withChecks((c) => setStatus(c, "residual_detection", "out_customers", { coverage: { rows_scanned: 0, rows_total: 0, columns: 2 } })), expect: blockedChecks("INCONCLUSIVE", "COVERAGE_INSUFFICIENT"), note: "a no-match PASS on a zero-row subject is INCONCLUSIVE" });
emit("k_partial_sample_pass", { statement: withChecks((c) => setStatus(c, "residual_detection", "out_customers", { coverage: { rows_scanned: 2, rows_total: 3, columns: 2 }, limits: { sampled: true, sample_rows: 2 } })), expect: blockedChecks("INCONCLUSIVE", "COVERAGE_INSUFFICIENT"), note: "a sample is never labeled a full scan" });
emit("k_coverage_inconsistent", { statement: withChecks((c) => setStatus(c, "residual_detection", "out_customers", { coverage: { rows_scanned: 9, rows_total: 3, columns: 2 } })), expect: malformed("STATEMENT_COVERAGE_INVALID") });
emit("k_warning_not_permitted", { statement: withChecks((c) => setStatus(c, "structural_integrity", "out_orders", { severity: "warning", reason_code: "SOFT_FINDING" })), expect: blockedChecks("FAIL", "WARNING_NOT_PERMITTED") });
emit("k_not_applicable_unjustified", { statement: withChecks((c) => setStatus(c, "structural_integrity", "quarantine", { status: "NOT_APPLICABLE", reason_code: "NO_RULES" })), expect: blockedChecks("INCONCLUSIVE", "NOT_APPLICABLE_UNJUSTIFIED"), note: "NOT_APPLICABLE needs a catalog rule, not a caller assertion" });
{
  const cat = structuredClone(CATALOG);
  cat.requirements = cat.requirements.map((r) => (r.check_id === "structural_integrity" ? { ...r, not_applicable_reason_codes: ["NO_RULES"] } : r));
  const f = catalogFile("catalog-na-allowed.json", cat);
  emit("k_not_applicable_justified", {
    catalog: f,
    statement: withChecks((c) => setStatus(c, "structural_integrity", "quarantine", { status: "NOT_APPLICABLE", reason_code: "NO_RULES" }), { policy: { id: cat.id, version: cat.version, digest: catDigest(cat) } }),
  });
  const empty = { ...CATALOG, requirements: [] };
  const fe = catalogFile("catalog-empty-forged.json", empty);
  emit("k_forged_empty_catalog", {
    statement: withChecks(() => [], { policy: { id: empty.id, version: empty.version, digest: catDigest(empty) } }),
    expect: blockedChecks("INCONCLUSIVE", "POLICY_DIGEST_MISMATCH"),
    note: "producer signs an empty catalog; the verifier uses its own independently trusted catalog",
  });
  void fe;
  const emptyOut = { ...CATALOG, empty_output_allowed: true, requirements: [REQ({ check_id: "source_snapshot_binding", scope: "snapshot", min_rows_scanned: 0, allow_empty_subject: true })] };
  const fo = catalogFile("catalog-empty-output-allowed.json", emptyOut);
  const noOut = (cat2) => withChecks(() => [mkCheck("source_snapshot_binding", "snapshot", "snap-001")], { artifacts: [], policy: { id: cat2.id, version: cat2.version, digest: catDigest(cat2) }, dispositions: { masking: "COMPLETE", output: "EMPTY_BY_POLICY" } });
  emit("o_empty_output_allowed", { catalog: fo, statement: noOut(emptyOut), ctx: { expected: { ...exp0, artifacts: [] }, artifact_ops: [{ op: "delete_all" }] }, note: "empty-output disposition explicitly allowed by policy" });
  const emptyOutDenied = { ...emptyOut, empty_output_allowed: false };
  const fd = catalogFile("catalog-empty-output-denied.json", emptyOutDenied);
  emit("o_empty_output_denied", { catalog: fd, statement: noOut(emptyOutDenied), ctx: { expected: { ...exp0, artifacts: [] }, artifact_ops: [{ op: "delete_all" }] }, expect: DENIED({ artifacts: "MISSING", exit: "INCOMPLETE", reasons_include: ["EMPTY_OUTPUT_NOT_ALLOWED"] }) });
}
emit("k_policy_id_version_mismatch", { statement: baseStatement({ policy: { id: "catalog-strict", version: "2", digest: catDigest(CATALOG) } }), expect: blockedChecks("INCONCLUSIVE", "POLICY_VERSION_MISMATCH") });
emit("k_snapshot_subject_mismatch", { statement: withChecks((c) => c.map((x) => (x.subject.kind === "snapshot" ? { ...x, subject: { kind: "snapshot", id: "snap-777" } } : x))), expect: blockedChecks("INCONCLUSIVE", "CHECK_SUBJECT_MISSING") });

// -------------------------------------------------------- standards vectors
// Generic JWS/JCS primitive vectors. They carry no Decoy headers and MUST NOT pass the Decoy profile.
{
  const a3 = {
    compact:
      "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ.DtEhU3ljbEg8L38VWAfUAqOyKAM6-Xx-F4GawxaepmXFCgfTjDxw5djxLa8ISlSApmWQxfKTUJqPP3-Kg6NU1Q",
    public_jwk: { kty: "EC", crv: "P-256", x: "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU", y: "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0" },
  };
  const [h, p, sg] = a3.compact.split(".");
  // independent confirmation that this is the RFC 7515 A.3 example and verifies
  const pub = await importJWK(a3.public_jwk, "ES256");
  const ok = await flattenedVerify({ protected: h, payload: p, signature: sg }, pub);
  if (!ok.payload.length) throw new Error("RFC 7515 A.3 vector failed to verify in jose");
  const rfc8785 = {
    sorting: {
      input_json: '{"\\u20ac":"Euro Sign","\\r":"Carriage Return","\\ufb33":"Hebrew Letter Dalet With Dagesh","1":"One","\\ud83d\\ude00":"Emoji: Grinning Face","\\u0080":"Control","\\u00f6":"Latin Small Letter O With Diaeresis"}',
      expected_canonical:
        '{"\\r":"Carriage Return","1":"One","\u0080":"Control","\u00f6":"Latin Small Letter O With Diaeresis","\u20ac":"Euro Sign","\ud83d\ude00":"Emoji: Grinning Face","\ufb33":"Hebrew Letter Dalet With Dagesh"}',
    },
    numbers: [
      ["0000000000000000", "0"], ["8000000000000000", "0"], ["0000000000000001", "5e-324"], ["8000000000000001", "-5e-324"],
      ["7fefffffffffffff", "1.7976931348623157e+308"], ["ffefffffffffffff", "-1.7976931348623157e+308"],
      ["4340000000000000", "9007199254740992"], ["c340000000000000", "-9007199254740992"], ["4430000000000000", "295147905179352830000"],
      ["44b52d02c7e14af5", "9.999999999999997e+22"], ["44b52d02c7e14af6", "1e+23"], ["44b52d02c7e14af7", "1.0000000000000001e+23"],
      ["444b1ae4d6e2ef4e", "999999999999999700000"], ["444b1ae4d6e2ef4f", "999999999999999900000"], ["444b1ae4d6e2ef50", "1e+21"],
      ["3eb0c6f7a0b5ed8c", "9.999999999999997e-7"], ["3eb0c6f7a0b5ed8d", "0.000001"],
      ["41b3de4355555553", "333333333.3333332"], ["41b3de4355555554", "333333333.33333325"], ["41b3de4355555555", "333333333.3333333"],
      ["41b3de4355555556", "333333333.3333334"], ["41b3de4355555557", "333333333.33333343"],
      ["becbf647612f3696", "-0.0000033333333333333333"], ["43143ff3c1cb0959", "1424953923781206.2"],
    ],
    cases: [
      { input_json: '{"b":2,"a":1}' },
      { input_json: '[1.0, 1e2, -0, 1E-7, 100000000000000000000, 1e21]' },
      { input_json: '{"s":"\\u0000\\u001f\\b\\t\\n\\f\\r\\"\\\\\\u007f\\u2028\\ud83d\\ude00"}' },
      { input_json: '{"\\u00e9":1,"e":2,"\\ud83d\\ude00":3,"\\uff21":4,"A":5}' },
      { input_json: '{"nested":{"z":[3,2,1],"a":{"y":null,"x":true}},"empty":{}}' },
    ],
  };
  for (const [hex, want] of rfc8785.numbers) {
    const dv = new DataView(new ArrayBuffer(8));
    dv.setBigUint64(0, BigInt("0x" + hex));
    const got = canonicalize(dv.getFloat64(0));
    if (got !== want) throw new Error(`RFC 8785 App. B ${hex}: independent impl gave ${got}, expected ${want}`);
  }
  if (canonicalize(JSON.parse(rfc8785.sorting.input_json)) !== rfc8785.sorting.expected_canonical) throw new Error("UTF-16 key order mismatch");
  for (const c of rfc8785.cases) c.expected = canonicalize(JSON.parse(c.input_json));
  putJson("standards.json", {
    note: "Generic primitive vectors. Expected values come from RFC text and an independent JCS implementation.",
    rfc7515_a3_es256: { ...a3, expected_math_valid: true, profile_must_reject: true },
    rfc8785,
  });
}

// ------------------------------------------------------------ corpus index
const files = [];
(function walk(d) {
  for (const n of readdirSync(d).sort()) {
    const p = join(d, n);
    if (statSync(p).isDirectory()) walk(p);
    else files.push(relative(OUT, p).split("\\").join("/"));
  }
})(OUT);
const lines = files.filter((f) => f !== "MANIFEST.json").map((f) => `${f}:${sha256hex(readFileSync(join(OUT, f)))}`);
putJson("MANIFEST.json", {
  corpus: "decoy-signed-evidence-conformance",
  version: "v1",
  generator: { language: "node", node: process.version, canonicalize: "5.1.0 (Apache-2.0, RFC 8785)", jose: "6.2.14 (MIT)", signing: "node:crypto OpenSSL ECDSA P-256/SHA-256 (ieee-p1363) and panva/jose FlattenedSign" },
  vector_count: vectors.length,
  vectors: vectors.map((v) => v.id),
  corpus_sha256: sha256hex(lines.join("\n")),
  files: Object.fromEntries(lines.map((l) => l.split(/:(?=[0-9a-f]{64}$)/))),
});
console.log(`wrote ${vectors.length} vectors, ${files.length} files to ${OUT}`);
