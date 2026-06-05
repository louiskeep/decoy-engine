# Quickstart

The shortest real path: install `decoy-engine`, describe a mask in a small
YAML config, and produce a masked CSV. This page uses the `decoy` CLI
(distributed as `decoy-cli`), which is the recommended way to drive the
engine. If you are embedding the engine in your own Python tool, see the
[recipes](recipes.md) for the in-process library calls.

## Install

```
pip install decoy-engine decoy-cli
```

Python 3.10, 3.11, and 3.12 are supported. The heavy dependencies (pandas,
Polars, PyArrow) are pulled in automatically.

## 1. A CSV to mask

Say you have `people.csv`:

```
first_name,last_name,email,ssn,account_status
Ada,Lovelace,ada@example.com,123-45-6789,active
Alan,Turing,alan@example.com,987-65-4321,closed
```

## 2. A pipeline config

Save this as `pipeline.yaml`. It names the source file, lists the columns to
mask and the strategy for each, and names the output file.

```yaml
version: 1

global_settings:
  seed: 42

sources:
  people:
    type: file
    format: csv
    path: ./people.csv

tables:
  - name: people
    columns:
      - name: first_name
        strategy: faker
        provider: person_first_name
      - name: last_name
        strategy: faker
        provider: person_last_name
      - name: email
        strategy: faker
        provider: person_email
      - name: ssn
        strategy: redact
      - name: account_status
        strategy: passthrough

targets:
  people:
    type: file
    format: csv
    path: ./people_masked.csv
```

`strategy: faker` swaps a value for a synthetic one from a named provider;
`redact` replaces every non-null value with a constant; `passthrough` leaves
the column untouched. The full catalog is in [strategies](strategies.md).

## 3. Validate, then run

```
decoy validate pipeline.yaml
decoy run pipeline.yaml
```

`validate` checks the config against the engine's schema before any data is
touched. `run` profiles the source, compiles a frozen plan, executes it on the
default (Polars) execution adapter, and writes `people_masked.csv`.

Because `global_settings.seed` is fixed, the same config plus the same input
produces the same output every time. For output that is stable across machines
(not just across runs on one box), supply a master key; see
[determinism](determinism.md).

## Where to go next

- [recipes](recipes.md): five runnable end-to-end recipes (folder masking with
  FK preservation, synthetic generation, PII detection, CI).
- [strategies](strategies.md): every mask and generation strategy.
- [relationships](relationships.md): how foreign keys survive a multi-table mask.
- [cli](cli.md): the full command surface.
