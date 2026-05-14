# Custom Providers

This folder holds user-defined word lists and name banks that the Decoy
engine exposes as **custom Faker providers**. Adding a file here is all
that is required to make a new provider available in pipeline YAML — no
code change needed.

## Folder convention

Place files directly inside this folder (sub-directories are ignored).
The engine scans the folder on startup and registers each file as a
provider named `custom.<stem>`, where `<stem>` is the filename without
its extension.

```
custom_providers/
    dog_breeds.txt          →  custom.dog_breeds
    internal_codes.json     →  custom.internal_codes
    product_lines.txt       →  custom.product_lines
```

## Supported file formats

### Plain text (`.txt`)

One value per line. Blank lines and lines beginning with `#` are ignored.

```
# dog_breeds.txt
Labrador Retriever
German Shepherd
Golden Retriever
French Bulldog
Bulldog
```

### JSON array (`.json`)

A top-level JSON array of strings.

```json
[
  "Widget Alpha",
  "Widget Beta",
  "Widget Gamma"
]
```

## Using a custom provider in a pipeline

### Masking YAML

```yaml
columns:
  - column: breed
    type: faker
    faker_type: "custom.dog_breeds"
```

### Generation YAML

```yaml
columns:
  - name: breed
    type: faker
    faker_type: "custom.dog_breeds"
```

## Environment variable override

The engine reads the `CUSTOM_PROVIDERS_DIR` environment variable to
locate the folder. When not set, it defaults to `custom_providers`
relative to the working directory (which is the repository root on
development machines and matches the platform’s `CUSTOM_PROVIDERS_DIR`
config setting).

```bash
export CUSTOM_PROVIDERS_DIR=/data/my_custom_providers
```

## Notes

- Provider names are case-sensitive: `custom.dog_breeds` and
  `custom.Dog_Breeds` are different providers.
- When two files resolve to the same stem (e.g. `codes.txt` and
  `codes.json`), the last one processed wins (files are scanned in
  alphabetical order).
- Selection within a list is deterministic when the pipeline has a
  key configured — same input value + same key always picks the same
  item from the list.
