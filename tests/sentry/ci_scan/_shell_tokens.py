"""Shell-command parsing for CI sentries: split step text into commands, normalise the tokens,
read pip and uv install arguments, find uv subcommands, tool fetches and index overrides.
Repository-neutral: no policy, no file names, no package names.

This file exists byte-identical in the CLI repository and the engine repository, at this path
(tests/sentry/ci_scan/_shell_tokens.py).
Each repository's vendored ledger records the other repository's commit that holds the same
bytes, and a test fetches that file and compares. That catches an edit made in one repository
only, even when the local ledger hash was recomputed. It does not catch a deliberate change that
lands in both repositories and re-points both ledgers; that is a reviewed change, not drift.

Every scanner built on it gets the same spellings: `/usr/bin/pip`, `pip3.11`, `--opt=value`,
glued short values (`-rfile`, `-e.`, `-ihttps://x`), global options before the subcommand
(`pip --python p install`, `uv --quiet sync`), and the body of `bash -c "..."`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SEPARATORS = {"&&", "||", ";", "|"}
# Options and environment variables that point an install at another package source.
INDEX_OPTIONS = {
    "--index-url",
    "--extra-index-url",
    "-i",
    "--index",
    "--default-index",
    "-f",
    "--find-links",
}
INDEX_ENV = {
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_INDEX",
    "UV_DEFAULT_INDEX",
}
# Install options that consume the next token and name no package; parse_install_args keeps them.
VALUE_OPTIONS = {
    "--python",
    "--build-constraint",
    "-t",
    "--target",
    "--prefix",
    "--no-binary",
    "--only-binary",
}
# Global options that may stand between `pip` or `uv` and the subcommand and take a value.
_PIP_GLOBAL_VALUE = {
    "--python",
    "-p",
    "--proxy",
    "--retries",
    "--timeout",
    "--exists-action",
    "--trusted-host",
    "--cert",
    "--client-cert",
    "--cache-dir",
    "--log",
    "--keyring-provider",
    "--use-feature",
    "--use-deprecated",
}
_UV_GLOBAL_VALUE = _PIP_GLOBAL_VALUE | {
    "--directory",
    "--project",
    "--config-file",
    "--color",
    "--python-preference",
    "--python-fetch",
    "--allow-insecure-host",
    "--env-file",
    "--preview-features",
    "--default-index",
    "--index",
    "--index-url",
    "-i",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--index-strategy",
    "--exclude-newer",
    "--resolution",
    "--prerelease",
    "--link-mode",
    "--python-platform",
    "--python-version",
}
# Use with fullmatch: `$` would also accept a trailing newline.
EXACT_PIN = re.compile(r"[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,.\-]+\])?==[^<>=!~\s]+")
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_TOOLS = {"uv", "uvx", "pipx", "python", "python3", "bash", "sh"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_UV_SUBCOMMANDS = {
    "sync", "add", "remove", "lock", "run", "tool", "pip", "venv", "export", "tree", "init",
    "build", "publish", "python", "cache", "self", "version", "format", "audit", "auth",
}  # fmt: skip
_SHORT_VALUE = re.compile(r"-[erific].+", re.S)
_SHELL_C = re.compile(r"-[A-Za-z]*c")


def is_pip(token: str) -> bool:
    return re.fullmatch(r"pip[0-9.]*", token) is not None


def base(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def code_lines(text: str) -> list[str]:
    """Shell lines with continuations joined and comment-only or blank lines dropped."""
    text = re.sub(r"\\\n\s*", " ", text)
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def normalise(cmd: list[str]) -> list[str]:
    """Give `/usr/local/bin/uv` and `pip3.11` their plain names; split `--opt=value`, `-e.`, `-rX`.

    Option splitting runs only for commands that name pip, uv, uvx or pipx, so `sed -i.bak` and
    `docker run -it` keep their shape."""
    named = [
        base(t)
        if "/" in t and not t.startswith("-") and (base(t) in _TOOLS or is_pip(base(t)))
        else t
        for t in cmd
    ]
    if not any(t in {"uv", "uvx", "pipx"} or is_pip(t) for t in named):
        return named
    out: list[str] = []
    for tok in named:
        if tok.startswith("--") and "=" in tok:
            key, _, value = tok.partition("=")
            out += [key, *([value] if value else [])]
        elif _SHORT_VALUE.fullmatch(tok):
            out += [tok[:2], tok[2:]]
        else:
            out.append(tok)
    return out


def _shell_body(cmd: list[str]) -> str | None:
    """The quoted script of `bash -c "..."`, or None when the command is not that."""
    if not cmd or base(cmd[0]) not in _SHELLS:
        return None
    for i, tok in enumerate(cmd[1:-1], start=1):
        if _SHELL_C.fullmatch(tok):
            return cmd[i + 1]
    return None


def commands(text: str) -> list[list[str]]:
    """Split step text into commands of normalised tokens: comments dropped, continuations joined,
    and the body of `bash -c "..."` split into commands of its own."""
    text = text.replace('\\"', '"').replace("\\$", "$")
    split: list[list[str]] = []
    for line in code_lines(text):
        current: list[str] = []
        for raw in _TOKEN.findall(line):
            token = raw.strip("\"'") if raw[0] in "\"'" else raw
            if token.endswith(";") and len(token) > 1:
                current.append(token[:-1])
                split.append(current)
                current = []
            elif token in SEPARATORS:
                split.append(current)
                current = []
            else:
                current.append(token)
        split.append(current)
    out: list[list[str]] = []
    for cmd in (normalise(c) for c in split if c):
        out.append(cmd)
        if (body := _shell_body(cmd)) is not None:
            out += commands(body)
    return out


def pip_install_args(cmd: list[str]) -> list[str] | None:
    """Arguments after `pip install` (pip, python -m pip, uv pip), or None if not one."""
    for i, tok in enumerate(cmd):
        if not is_pip(tok):
            continue
        rest = cmd[i + 1 :]
        j = _skip_options(rest, 0, _PIP_GLOBAL_VALUE)
        if j < len(rest) and rest[j] == "install":
            return rest[j + 1 :]
    return None


def _skip_options(tokens: list[str], start: int, with_value: set[str]) -> int:
    """Index of the first token at or after `start` that is not an option or an option's value."""
    j = start
    while j < len(tokens) and tokens[j].startswith("-"):
        j += 2 if tokens[j] in with_value else 1
    return j


def _uv_positions(cmd: list[str]) -> list[int]:
    """Indexes in `cmd` of the candidates for the uv subcommand. Normally one: the first
    positional after the global options, which are skipped with their values, plus any known
    subcommand names that follow it directly. If that positional is not a known subcommand, an option this scanner does not know took a value
    (`uv --some-new-option x sync`), so every known subcommand after `uv` is a candidate and a
    scanner that flags any of them fails closed."""
    if "uv" not in cmd:
        return []
    start = cmd.index("uv") + 1
    j = _skip_options(cmd, start, _UV_GLOBAL_VALUE)
    if j < len(cmd) and cmd[j] in _UV_SUBCOMMANDS:
        # Known subcommand names right after it may be the real one if an unknown option took
        # the first as its value (`uv --new-option run sync`).
        run = [j]
        while run[-1] + 1 < len(cmd) and cmd[run[-1] + 1] in _UV_SUBCOMMANDS:
            run.append(run[-1] + 1)
        return run
    found = [k for k in range(start, len(cmd)) if cmd[k] in _UV_SUBCOMMANDS]
    return found or ([j] if j < len(cmd) else [])


def _uv_name(cmd: list[str], pos: int) -> str:
    if cmd[pos] != "pip":
        return cmd[pos]
    k = _skip_options(cmd, pos + 1, _PIP_GLOBAL_VALUE)
    return f"pip {cmd[k]}" if k < len(cmd) else "pip"


def uv_subcommands(cmd: list[str]) -> list[str]:
    """Every candidate uv subcommand (see `_uv_positions`): `sync`, `add`, `lock`, `run`, `tool`, or
    for pip `pip install` / `pip sync`. Empty when the command does not run uv."""
    return [_uv_name(cmd, pos) for pos in _uv_positions(cmd)]


def uv_subcommand(cmd: list[str]) -> str | None:
    """The first candidate of `uv_subcommands`, or None when the command does not run uv."""
    names = uv_subcommands(cmd)
    return names[0] if names else None


def is_package_command(cmd: list[str]) -> bool:
    """True when the command runs pip, uv, uvx or pipx, the only commands whose `-f`, `-i` and
    index options mean a package source (`rm -f`, `tail -f` and `docker build -f` do not)."""
    return any(t in {"uv", "uvx", "pipx"} or is_pip(t) for t in cmd)


# Verbs that fetch packages from an index. For uv the rule fails closed: any `uv tool` verb that is
# not known to fetch nothing counts as a fetch. `pipx` lists the fetching verbs it knows.
_UV_TOOL_NO_FETCH = {"list", "dir", "uninstall", "update-shell"}
_PIPX_FETCH = {"install", "run", "inject", "upgrade", "upgrade-all", "reinstall", "reinstall-all"}


def _uv_tool_verb(cmd: list[str], pos: int) -> tuple[str, int]:
    """The `uv tool` verb and its index; options before it are skipped with their values."""
    k = _skip_options(cmd, pos + 1, _UV_GLOBAL_VALUE)
    return (cmd[k], k) if k < len(cmd) else ("", k)


def _uv_tool_fetches(cmd: list[str], pos: int) -> tuple[str, int] | None:
    verb, k = _uv_tool_verb(cmd, pos)
    return None if not verb or verb in _UV_TOOL_NO_FETCH else (verb, k)


def tool_fetch(cmd: list[str]) -> tuple[str, list[str]] | None:
    """(label, arguments) when the command fetches a tool from an index: `uvx`, any `uv tool` verb
    not known to be harmless (so `uv tool install|run|upgrade`, and an unrecognised verb), and the
    pipx verbs that install or upgrade (`runpip ... install` too). None for the rest, so
    `uv tool list|dir|uninstall` and `pipx list` stay clean."""
    if "uvx" in cmd:
        return "uvx", cmd[cmd.index("uvx") + 1 :]
    for pos in _uv_positions(cmd):
        if cmd[pos] == "tool" and (fetch := _uv_tool_fetches(cmd, pos)):
            return f"uv tool {fetch[0]}", cmd[fetch[1] + 1 :]
    if "pipx" in cmd:
        i = cmd.index("pipx")
        k = _skip_options(cmd, i + 1, set())
        verb = cmd[k] if k < len(cmd) else ""
        if verb in _PIPX_FETCH:
            return f"pipx {verb}", cmd[k + 1 :]
        if verb == "runpip" and "install" in cmd[k + 1 :]:
            return "pipx runpip install", cmd[k + 1 :]
    return None


def index_env_names(tokens: list[str]) -> list[str]:
    """Index environment variables assigned in a command (`PIP_INDEX_URL=x pip install`,
    `export UV_INDEX_URL=x`)."""
    names = (t.split("=", 1)[0] for t in tokens if "=" in t)
    return [n for n in names if n in INDEX_ENV]


def env_mapping_index_names(env: object) -> list[str]:
    """Index environment variables set in a workflow, job or step `env:` mapping."""
    return [k for k in env if k in INDEX_ENV] if isinstance(env, dict) else []


@dataclass
class InstallArgs:
    """What one `pip install` names, sorted by role. Policy decides which of it is allowed."""

    constraints: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    index_overrides: list[str] = field(default_factory=list)
    editables: list[str] = field(default_factory=list)
    positionals: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    values: dict[str, list[str]] = field(default_factory=dict)


def parse_install_args(args: list[str]) -> InstallArgs:
    """Sort install arguments: option values are consumed by their option, never read as packages."""
    parsed = InstallArgs()
    it = iter(args)
    for arg in it:
        if arg in {"-c", "--constraint"}:
            parsed.constraints.append(next(it, ""))
        elif arg in {"-r", "--requirement"}:
            parsed.requirements.append(next(it, ""))
        elif arg in INDEX_OPTIONS:
            parsed.index_overrides.append(arg)
            next(it, None)
        elif arg in VALUE_OPTIONS:
            parsed.values.setdefault(arg, []).append(next(it, ""))
        elif arg in {"-e", "--editable"}:
            parsed.editables.append(next(it, ""))
        elif arg.startswith("-"):
            parsed.flags.add(arg)
        else:
            parsed.positionals.append(arg)
    return parsed


def uv_run_findings(cmd: list[str], *, frozen_env: bool = False) -> list[str]:
    """`uv run` and `uv tool run` resolve and install on their own: `--with*` adds packages, and
    without `--frozen` (or UV_FROZEN=1 in the environment) they may re-lock. A `uv tool` command
    whose verb is unclear but contains `run` counts as `uv tool run`."""

    def runs(pos: int) -> bool:
        if cmd[pos] == "run":
            return True
        if cmd[pos] != "tool" or not (fetch := _uv_tool_fetches(cmd, pos)):
            return False
        return fetch[0] == "run" or "run" in cmd[fetch[1] :]

    if not any(runs(pos) for pos in _uv_positions(cmd)):
        return []
    after = cmd[cmd.index("uv") + 1 :]
    found = []
    if any(t.startswith("--with") for t in after):
        found.append("uv run --with installs outside the lock")
    if "--frozen" not in after and not frozen_env:
        found.append("uv run without --frozen and no UV_FROZEN env")
    return found
