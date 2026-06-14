# Agent Guide

Operating guide for coding agents working in the `decoy-engine` repo.

## Reading order

1. [README.md](README.md) for what the engine is and the public API entrypoints.
2. [CODEMAP.md](CODEMAP.md) for the package layout and "Where Do I Find" pointers.
3. [CONTRIBUTING.md](CONTRIBUTING.md) for build/test conventions.
4. [CLAUDE.md](CLAUDE.md) for agent-specific best-practice notes.
5. [SECURITY.md](SECURITY.md) for the security posture and reporting channel.

## Role split

The main session is the developer. Reviews are handled by the `dennis` subagent (`~/.claude/agents/dennis.md`): he commands the quality gate (runs `/code-review`, `/security-review`, and `/qa`), cold-reads the diff himself, and returns one adversarial verdict with severity tiers. He does not write fixes or merge. Documentation is handled by the `barry` subagent (`~/.claude/agents/barry.md`): he drives the gstack `document-*` skills to keep the doc set true to what shipped, and does not change logic. Delegate to them rather than self-reviewing or hand-writing docs in the main session.

## Automatic quality gate

`git push` and `gh pr create` are blocked by a `PreToolUse` hook (`.claude/hooks/decoy-quality-gate.sh`) until the gate has passed for the exact current HEAD. To clear it:

1. Dispatch `dennis` to review the change; resolve every BLOCKER and HIGH.
2. Dispatch `barry` to sync the docs to the change.
3. Record the pass: `bash .claude/hooks/decoy-gate-pass.sh`.

The receipt is the commit SHA, so any new commit re-arms the gate. This is the local standard; GitHub branch protection is the complementary server-side gate.

## Scope of this repo

`decoy-engine` is library code. It has no network surface, no auth boundary, and no background process of its own. It runs inside the caller's Python process with the caller's privileges. CLI and platform helpers live in their own repos.

---

Full agent-guide content lives in the commercial platform repo.
