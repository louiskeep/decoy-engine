# Agent Guide

Operating guide for coding agents working in the `decoy-engine` repo.

## Reading order

1. [README.md](README.md) for what the engine is and the public API entrypoints.
2. [CODEMAP.md](CODEMAP.md) for the package layout and "Where Do I Find" pointers.
3. [CONTRIBUTING.md](CONTRIBUTING.md) for build/test conventions.
4. [CLAUDE.md](CLAUDE.md) for agent-specific best-practice notes.
5. [SECURITY.md](SECURITY.md) for the security posture and reporting channel.

## Role split

The main session is the developer. Tech-lead reviews are handled by the `dennis` subagent (`~/.claude/agents/dennis.md`); delegate to him rather than performing reviews in the main session. Docs and code-hygiene passes go to the `barry` subagent (`~/.claude/agents/barry.md`).

## Scope of this repo

`decoy-engine` is library code. It has no network surface, no auth boundary, and no background process of its own. It runs inside the caller's Python process with the caller's privileges. CLI and platform helpers live in their own repos.

---

Full agent-guide content lives in the commercial platform repo.
