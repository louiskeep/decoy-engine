# CAM-STEPS: activating the CI/perf tooling

Manual steps to turn on each of the three integrations wired on
`feat/perf-harness-and-ci-insights`. Every one of them is a no-op right now.
Do them in this order; each is independent, so skipping one does not block
the others.

## 1. CodSpeed performance harness

What it is: instrumented microbenchmarks in `tests/codspeed/`, wired to
`.github/workflows/codspeed.yml`.

Steps:
1. Go to codspeed.io and sign in with GitHub, then link the
   `louiskeep/decoy-engine` repo.
2. CodSpeed gives you a token. In GitHub: Settings > Secrets and variables >
   Actions > Secrets > New repository secret.
3. Name it exactly `CODSPEED_TOKEN`, paste the value, save.

Verify it is live:
- Open a PR that touches `src/decoy_engine/**` or `tests/codspeed/**`, or run
  the `codspeed` workflow manually from the Actions tab (Run workflow
  button). The `benchmarks` job should now run instead of being skipped, and
  results should show up on your CodSpeed dashboard within a few minutes.
- Before the secret is set, the same workflow run shows the `benchmarks` job
  as skipped, not failed. That is the expected no-op state.

Disable / roll back:
- Remove the `CODSPEED_TOKEN` secret. The job goes back to skipped on the
  next run. No code change needed.

## 2. Mergify merge queue + JUnit results

What it is: `ci.yml`'s `regression-gate` job now writes a JUnit XML report
and uploads it as a workflow artifact. `.mergify.yml` at the repo root has a
starter merge-queue config that only fires once the app is installed.

Steps:
1. Go to github.com/apps/mergify and install the Mergify app on the
   `louiskeep/decoy-engine` repo (or your whole GitHub account/org, scoped to
   this repo). This is the only step required; no secret to create.
2. Optional, once you are ready to use the queue day to day: confirm the
   required-check names in `.mergify.yml` (`ci / ruff`, `ci / mypy`,
   `ci / regression-gate`) still match what you actually require in
   Settings > Branches for the `main` branch protection rule, and adjust the
   list if you have changed required checks since this was written.

Verify it is live:
- After installing the app, open any PR. Mergify should register as a check
  and, once you add the `queue` label to an approved PR, it should pick it
  up per `.mergify.yml`'s `default` queue rule.
- The JUnit artifact itself is already uploading on every `ci` run right
  now, no matter what: open a recent `ci` run's `regression-gate` job and
  look for the `junit-results` artifact in the run summary. That part needs
  no action from you; it silently produces the artifact whether or not
  Mergify is installed, and Mergify starts consuming it automatically once
  it has repo access.

Disable / roll back:
- Uninstall the Mergify app (Settings > Integrations > Mergify > Configure >
  Uninstall), or delete the `queue` label so no PR can enter the queue. The
  JUnit upload step in `ci.yml` is harmless to leave in place either way; if
  you want to remove it, delete the `--junitxml=...` flag and the "Upload
  JUnit results" step from `regression-gate` in `.github/workflows/ci.yml`.

## 3. GitHub Pages docs deploy

Found already built on `main`, not something new on this branch:
`.github/workflows/docs.yml` already has a `build` job (runs on every push/PR
touching docs) and a `deploy` job gated on
`vars.DOCS_DEPLOY == 'true'` plus push-to-main. See that file's own header
comment for the full detail. This branch does not add a second Pages
workflow; adding one would collide with the deploy concurrency group
`docs.yml` already uses. If you specifically want a `workflow_dispatch`-only
variant instead of the existing push-triggered build, say so and it can be
adjusted; as shipped it matches the same "inert until enabled" contract this
task asked for, just implemented earlier.

Steps to turn it on:
1. In GitHub: Settings > Pages > Source: set to "GitHub Actions". (This repo
   is private; Pages on a private repo needs GitHub Pro/Team/Enterprise, or
   make the repo public first. If neither applies yet, stop here and leave
   `DOCS_DEPLOY` unset; the `build` job still runs and uploads the built
   HTML as a downloadable artifact.)
2. In GitHub: Settings > Secrets and variables > Actions > Variables > New
   repository variable.
3. Name it exactly `DOCS_DEPLOY`, value `true`.

Verify it is live:
- Push to `main` (or merge a PR). The `docs` workflow's `deploy` job should
  run and publish to `https://<org>.github.io/decoy-engine/` (or your custom
  Pages domain if configured).
- Before `DOCS_DEPLOY` is set to `true`, `deploy` is skipped; `build` still
  runs and its HTML is downloadable from the workflow run's artifacts.

Disable / roll back:
- Set `DOCS_DEPLOY` back to anything other than `true` (or delete the
  variable). The next push to `main` will skip `deploy`. This does not take
  an already-published Pages site down; to do that, go to Settings > Pages
  and remove the source.
