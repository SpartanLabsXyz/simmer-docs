#!/usr/bin/env python3
"""Bind every documented SDK example against installed SDK signatures.

Why this exists
---------------
Doc examples drift from the shipped package. The worst case is a code block that
uses a kwarg (or method) the installed wheel rejects — a builder copies it, runs
it, and gets a TypeError. Doc-vs-doc linting can't catch this; only the real
package signature can. CI runs this checker twice: once with ``simmer-sdk``
installed at the doc-declared floor version, and once with the latest published
SDK. That catches examples that need newer-than-advertised APIs and examples
that break after a later SDK signature change.

Safety
------
It NEVER executes example code. It parses each python block with ``ast`` and uses
``inspect.signature(...).bind_partial(...)`` with placeholder values — so it needs
no API key, no network (after the one install), and can't trigger a trade. A
kwarg the shipped wheel doesn't accept raises ``TypeError`` at bind time; that's
the failure we report.

Scope / conventions
-------------------
- Scans ``*.mdx`` (recursively) for ```python / ```py fenced blocks (indentation
  inside MDX components is handled).
- Tracks ``var = SimmerClient(...)`` / ``.from_env()`` / ``.with_ows_wallet()``
  assignments per file, and seeds the common doc convention ``client`` ->
  SimmerClient, ``gamma`` -> GammaClient so blocks that reuse a client defined in
  an earlier block still get checked.
- Only instance-method calls on a mapped client var are checked. Anything
  uncertain (unmapped var, ``*args``/``**kwargs`` splat, unparseable fragment) is
  skipped — the gate biases to no-false-positives.

Escape hatch
------------
Put ``{/* bind:skip */}`` on the line immediately before a fence to exclude an
intentionally-illustrative / pseudo-code block. Put
``{/* bind:floor=X.Y.Z */}`` before a fence to check that block against a higher
floor than the repo default (e.g. a feature that shipped in a later SDK release).

Use MDX comment syntax, NOT ``<!-- -->``. Mintlify's parser rejects HTML comments
outright ("Unexpected character `!` (U+0021) before name") and fails the whole
deployment, which leaves docs.simmer.markets frozen at the last good build. The
markers are matched as substrings, so either syntax satisfies this script -- only
one of them survives production.

Usage
-----
    python scripts/check_sdk_examples.py [paths...]   # default: scan cwd
    python scripts/check_sdk_examples.py --floor 0.20.3

Exit code 1 if any example fails to bind.
"""
from __future__ import annotations

import argparse
import ast
import inspect
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

# Client classes we know how to introspect. Map a var to one of these and we'll
# validate calls on it against the real shipped signature.
CLIENT_CLASS_NAMES = {"SimmerClient", "GammaClient"}
# Common doc convention: these var names default to a client class unless an
# explicit assignment in the same file says otherwise.
DEFAULT_VAR_CLASS = {"client": "SimmerClient", "c": "SimmerClient", "gamma": "GammaClient"}

FENCE_RE = re.compile(r"^(\s*)```\s*(\w+)")
SKIP_RE = re.compile(r"bind:skip")
FLOOR_RE = re.compile(r"bind:floor=([0-9][0-9A-Za-z.\-]*)")

_PLACEHOLDER = object()


@dataclass
class Block:
    path: Path
    start_line: int  # 1-based file line of the ```python fence
    code: str
    floor_override: str | None


@dataclass
class Violation:
    path: Path
    line: int
    call: str
    reason: str
    floor: str


def extract_blocks(path: Path) -> list[Block]:
    """Pull python fenced blocks out of an .mdx file, honoring skip/floor markers."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        m = FENCE_RE.match(lines[i])
        if not m or m.group(2).lower() not in ("python", "py"):
            i += 1
            continue
        indent = m.group(1)
        fence_line = i + 1  # 1-based
        # Look back at the nearest non-blank line for markers.
        skip = False
        floor_override = None
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0:
            if SKIP_RE.search(lines[j]):
                skip = True
            fm = FLOOR_RE.search(lines[j])
            if fm:
                floor_override = fm.group(1)
        # Collect until the closing fence (same-or-less indent ``` with no lang).
        body: list[str] = []
        i += 1
        while i < n:
            stripped = lines[i].strip()
            if stripped.startswith("```"):
                break
            body.append(lines[i])
            i += 1
        i += 1  # skip closing fence
        if skip:
            continue
        code = textwrap.dedent("\n".join(body))
        blocks.append(Block(path=path, start_line=fence_line, code=code, floor_override=floor_override))
    return blocks


def _rhs_class(node: ast.AST) -> str | None:
    """If an assignment RHS is a call constructing/sugaring a known client, name it."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id in CLIENT_CLASS_NAMES:
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in CLIENT_CLASS_NAMES:  # SimmerClient.from_env(...)
            return func.value.id
    return None


def _call_str(var: str, method: str, node: ast.Call) -> str:
    parts = [ast.unparse(a) for a in node.args]
    parts += [f"{kw.arg}=..." if kw.arg else "**..." for kw in node.keywords]
    return f"{var}.{method}({', '.join(parts)})"


def bind_check(cls: type, method: str, node: ast.Call) -> str | None:
    """Return a failure reason if the call can't bind to the real signature, else None."""
    raw = inspect.getattr_static(cls, method, None)
    if raw is None:
        return f"{cls.__name__} has no attribute '{method}'"
    if isinstance(raw, staticmethod):
        func, self_offset = raw.__func__, 0
    elif isinstance(raw, classmethod):
        func, self_offset = getattr(cls, method), 0
    elif inspect.isfunction(raw):
        func, self_offset = raw, 1  # plain instance method: signature includes self
    else:
        return None  # property / descriptor we can't statically bind — skip
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return None
    # Splat args/kwargs make static arity unknowable — don't risk a false positive.
    if any(isinstance(a, ast.Starred) for a in node.args):
        return None
    if any(kw.arg is None for kw in node.keywords):
        return None
    pos = [_PLACEHOLDER] * (len(node.args) + self_offset)
    kwargs = {kw.arg: _PLACEHOLDER for kw in node.keywords}
    try:
        sig.bind_partial(*pos, **kwargs)
    except TypeError as e:
        return str(e)
    return None


FOREIGN_CLIENT_RE = re.compile(r"\bpy_clob_client\w*\b")


def _numeric_version(value: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+(?:\.\d+)*)", value)
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def _version_lt(left: str, right: str) -> bool:
    left_parts = _numeric_version(left)
    right_parts = _numeric_version(right)
    if left_parts is None or right_parts is None:
        return False
    width = max(len(left_parts), len(right_parts))
    return left_parts + (0,) * (width - len(left_parts)) < right_parts + (0,) * (width - len(right_parts))


def check_block(
    block: Block, classes: dict[str, type], default_floor: str, seed_defaults: bool, installed_sdk: str | None = None
) -> list[Violation]:
    floor = block.floor_override or default_floor
    if block.floor_override and installed_sdk and _version_lt(installed_sdk, block.floor_override):
        return []
    try:
        tree = ast.parse(block.code)
    except SyntaxError:
        return []  # fragment / pseudo-code — not bindable, skip silently
    # The bare-`client` convention is only safe where a SimmerClient is actually in
    # play. `seed_defaults` is the file-level signal (the file imports simmer_sdk);
    # a block that pulls in a foreign client lib (e.g. py_clob_client in the V2
    # migration guide) re-binds `client` to something else, so suppress there too.
    var_class: dict[str, str] = {}
    if seed_defaults and not FOREIGN_CLIENT_RE.search(block.code):
        var_class.update(DEFAULT_VAR_CLASS)
    # Explicit assignments always win (unambiguous, even in non-simmer files).
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            cls_name = _rhs_class(node.value)
            if cls_name:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        var_class[tgt.id] = cls_name
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        var = func.value.id
        cls_name = var_class.get(var)
        if not cls_name:
            continue
        cls = classes.get(cls_name)
        if cls is None:
            continue  # class not importable in this SDK version — skip
        reason = bind_check(cls, func.attr, node)
        if reason:
            violations.append(
                Violation(
                    path=block.path,
                    line=block.start_line + node.lineno,
                    call=_call_str(var, func.attr, node),
                    reason=reason,
                    floor=floor,
                )
            )
    return violations


def load_classes() -> dict[str, type]:
    classes: dict[str, type] = {}
    try:
        from simmer_sdk import SimmerClient  # type: ignore

        classes["SimmerClient"] = SimmerClient
    except Exception as e:  # pragma: no cover - import guard
        print(f"FATAL: could not import SimmerClient from simmer_sdk: {e}", file=sys.stderr)
        sys.exit(2)
    try:  # GammaClient is optional / may live under a submodule
        from simmer_sdk import GammaClient  # type: ignore

        classes["GammaClient"] = GammaClient
    except Exception:
        pass
    return classes


def installed_version() -> str:
    try:
        from importlib.metadata import version

        return version("simmer-sdk")
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="files or dirs to scan (default: cwd)")
    parser.add_argument("--floor", help="override the repo floor (default: read .sdk-doc-floor)")
    parser.add_argument(
        "--target",
        choices=("floor", "latest"),
        default="floor",
        help="installed SDK target being checked; controls diagnostics only",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    if args.floor:
        floor = args.floor
    else:
        floor_file = repo_root / ".sdk-doc-floor"
        floor = floor_file.read_text().strip() if floor_file.exists() else "unknown"

    installed = installed_version()
    target_label = f"declared floor {floor}" if args.target == "floor" else f"latest installed SDK {installed}"
    print(f"checking SDK doc examples — {target_label}, installed simmer-sdk {installed}")
    if args.target == "floor" and floor != "unknown" and installed != "unknown" and installed != floor:
        print(
            f"WARNING: installed simmer-sdk {installed} != declared floor {floor}. "
            f"CI should `pip install simmer-sdk=={floor}` so this gate tests the floor.",
            file=sys.stderr,
        )

    targets = [Path(p) for p in args.paths] if args.paths else [repo_root]
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files.extend(sorted(t.rglob("*.mdx")))
        elif t.suffix == ".mdx":
            files.append(t)

    classes = load_classes()
    all_violations: list[Violation] = []
    n_blocks = 0
    establishes_re = re.compile(r"\bsimmer_sdk\b|SimmerClient\s*[(.]")
    for f in files:
        seed_defaults = bool(establishes_re.search(f.read_text(encoding="utf-8")))
        for block in extract_blocks(f):
            n_blocks += 1
            all_violations.extend(check_block(block, classes, floor, seed_defaults, installed))

    print(f"scanned {len(files)} files, {n_blocks} python blocks")
    if not all_violations:
        print(f"OK — all documented SDK calls bind against the {args.target} signature.")
        return 0

    reported_version = floor if args.target == "floor" else installed
    print(f"\nFAIL — {len(all_violations)} example call(s) do not bind against simmer-sdk {reported_version}:\n")
    for v in all_violations:
        rel = v.path.relative_to(repo_root) if v.path.is_absolute() else v.path
        print(f"  {rel}:{v.line}")
        print(f"    {v.call}")
        print(f"    -> {v.reason} (floor {v.floor})")
    print(
        "\nFix: correct the example, add `{/* bind:floor=X.Y.Z */}` if it needs a newer SDK,\n"
        "or `{/* bind:skip */}` if it is intentionally illustrative."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
