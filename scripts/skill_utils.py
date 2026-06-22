#!/usr/bin/env python3
"""Shared strict utilities for create-harness tools.

Target skills are untrusted data. Nothing here imports or executes target code.
Requires Python 3.8+ and PyYAML 6+.
"""
from __future__ import annotations

import ast
import hashlib
import html
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

MANIFEST_NAME = "SHA256SUMS.txt"
RESOURCE_PREFIXES = ("scripts/", "references/", "assets/", "agents/")
TOP_FILES = {
    "SKILL.md", "README.md", "BUILD_RECEIPT.md", "CHANGELOG.md",
    "LICENSE", "LICENSE.md", "LICENSE.txt", MANIFEST_NAME,
}
DISALLOWED_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "__pycache__", "node_modules",
}
DISALLOWED_NAMES = {".DS_Store"}
DISALLOWED_SUFFIXES = {".pyc", ".pyo", ".skill"}
SCRIPT_LANG = {
    ".py": "python", ".sh": "shell", ".bash": "shell",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
}
SHELL_META_RE = re.compile(r"[;&|><`$\r\n]")
PYTHON_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.I)
DRIVE_RE = re.compile(r"^[A-Za-z]:")


class SkillToolError(Exception):
    pass


class DependencyError(SkillToolError):
    pass


class FrontmatterError(SkillToolError):
    pass


class PathPolicyError(SkillToolError):
    pass


class CommandPolicyError(SkillToolError):
    pass


@dataclass(frozen=True)
class FrontmatterDocument:
    data: Mapping[str, object]
    body: str
    yaml_text: str


@dataclass(frozen=True)
class ParsedSelfTest:
    command: str
    argv: Tuple[str, ...]
    executable: str
    target_rel: str
    language: str
    direct_script: bool


def require_yaml():
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise DependencyError(
            "PyYAML is required; install it with 'python3 -m pip install PyYAML>=6'"
        ) from exc
    try:
        major = int(str(getattr(yaml, "__version__", "0")).split(".", 1)[0])
    except ValueError:
        major = 0
    if major < 6:
        raise DependencyError("PyYAML 6 or newer is required")
    return yaml


def _strict_loader(yaml):
    class StrictSafeLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found an unhashable mapping key", key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    StrictSafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    return StrictSafeLoader


def parse_frontmatter(text: str) -> FrontmatterDocument:
    yaml = require_yaml()
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0] != "---":
        raise FrontmatterError("no YAML frontmatter (first line must be exactly '---')")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise FrontmatterError("malformed frontmatter fence (missing closing '---')") from exc
    block = "\n".join(lines[1:closing])
    try:
        data = yaml.load(block, Loader=_strict_loader(yaml))
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 2}, column {mark.column + 1}" if mark else ""
        raise FrontmatterError(f"invalid YAML{where}: {problem}") from exc
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    return FrontmatterDocument(data, "\n".join(lines[closing + 1:]), block)


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillToolError(f"not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SkillToolError(f"could not read {path}: {exc}") from exc


def read_skill_document(skill_dir: Path) -> FrontmatterDocument:
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        raise SkillToolError("SKILL.md not found")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise SkillToolError("SKILL.md is unreasonably large (>4 MiB)")
    return parse_frontmatter(read_utf8(path))


def normalize_relative_path(
    raw: str, *, decode_markdown_url: bool = False, require_dot_prefix: bool = False
) -> str:
    if not isinstance(raw, str):
        raise PathPolicyError("path is not a string")
    value = html.unescape(raw).strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if decode_markdown_url:
        value = unquote(value).split("#", 1)[0]
    if require_dot_prefix:
        if not value.startswith("./"):
            raise PathPolicyError("manifest path must start with './'")
        value = value[2:]
    elif value.startswith("./"):
        value = value[2:]
    if not value:
        raise PathPolicyError("path is empty")
    if any(ch in value for ch in ("\x00", "\n", "\r")):
        raise PathPolicyError("path contains a control character")
    if "\\" in value:
        raise PathPolicyError("path must use POSIX '/' separators")
    if value.startswith(("/", "~")) or DRIVE_RE.match(value):
        raise PathPolicyError("absolute and home-relative paths are not allowed")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise PathPolicyError("URL paths are not local skill resources")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PathPolicyError("path contains an empty, '.', or '..' segment")
    canonical = PurePosixPath(*parts).as_posix()
    if canonical != value:
        raise PathPolicyError(f"path is not canonical (expected '{canonical}')")
    return canonical


def resolve_inside(
    root: Path, raw: str, *, must_exist: bool = False, require_file: bool = False,
    decode_markdown_url: bool = False, require_dot_prefix: bool = False,
) -> Tuple[str, Path]:
    root_real = root.resolve(strict=True)
    rel = normalize_relative_path(
        raw, decode_markdown_url=decode_markdown_url,
        require_dot_prefix=require_dot_prefix,
    )
    current = root_real
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            raise PathPolicyError(
                f"symlinked path component is not allowed: {current.relative_to(root_real)}"
            )
        if not current.exists():
            break
    target = root_real.joinpath(*PurePosixPath(rel).parts)
    resolved = target.resolve(strict=False)
    try:
        common = os.path.commonpath([str(root_real), str(resolved)])
    except ValueError as exc:
        raise PathPolicyError("path is on a different filesystem root") from exc
    if common != str(root_real):
        raise PathPolicyError("path escapes the skill root")
    if must_exist and not target.exists():
        raise PathPolicyError("path does not exist")
    if require_file and (not target.exists() or not target.is_file()):
        raise PathPolicyError("path is not a regular file")
    return rel, target


def find_symlinks(root: Path, *, skip_disallowed: bool = True) -> List[str]:
    found: List[str] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SkillToolError(f"could not scan {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if entry.is_symlink():
                found.append(rel)
            elif entry.is_dir(follow_symlinks=False):
                if not (skip_disallowed and entry.name in DISALLOWED_DIRS):
                    stack.append(path)
    return sorted(found)


def find_disallowed_package_artifacts(root: Path) -> List[str]:
    found: List[str] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SkillToolError(f"could not scan {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in DISALLOWED_DIRS:
                    found.append(rel + "/")
                else:
                    stack.append(path)
            elif (
                entry.name in DISALLOWED_NAMES
                or entry.name.startswith(".SHA256SUMS.")
                or path.suffix in DISALLOWED_SUFFIXES
            ):
                found.append(rel)
    return sorted(found)


def iter_regular_files(root: Path, *, skip_disallowed: bool = False) -> Iterable[Tuple[str, Path]]:
    rows: List[Tuple[str, Path]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SkillToolError(f"could not scan {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if entry.is_symlink():
                raise PathPolicyError(f"symlinks are not allowed in skills: {rel}")
            if entry.is_dir(follow_symlinks=False):
                if skip_disallowed and entry.name in DISALLOWED_DIRS:
                    continue
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                if skip_disallowed and (
                    entry.name in DISALLOWED_NAMES or path.suffix in DISALLOWED_SUFFIXES
                ):
                    continue
                rows.append((rel, path))
            else:
                raise PathPolicyError(f"non-regular filesystem entry is not allowed: {rel}")
    yield from sorted(rows, key=lambda item: item[0])


def manifest_files(root: Path) -> List[Tuple[str, Path]]:
    bad = find_disallowed_package_artifacts(root)
    if bad:
        raise PathPolicyError(
            "package contains cache/VCS/nested-archive artifacts: " + ", ".join(bad[:20])
        )
    rows: List[Tuple[str, Path]] = []
    for rel, path in iter_regular_files(root):
        if rel == MANIFEST_NAME:
            continue
        canonical = normalize_relative_path(rel)
        if canonical != rel:
            raise PathPolicyError(f"package path is not canonical: {rel!r}")
        rows.append((rel, path))
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SkillToolError(f"could not hash {path}: {exc}") from exc
    return digest.hexdigest()


def _shebang(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.readline(256).decode("utf-8", errors="ignore").strip().lower()
    except OSError:
        return ""


def script_language(path: Path) -> Optional[str]:
    known = SCRIPT_LANG.get(path.suffix.lower())
    if known:
        return known
    first = _shebang(path)
    if not first.startswith("#!"):
        return None
    if "python" in first:
        return "python"
    if re.search(r"\b(?:ba|z|k)?sh\b", first):
        return "shell"
    if "node" in first or "deno" in first:
        return "javascript"
    return "unknown"


def discover_scripts(skill_dir: Path) -> List[Tuple[str, Path, str]]:
    directory = skill_dir / "scripts"
    if not directory.is_dir():
        return []
    rows = []
    for rel, path in iter_regular_files(directory, skip_disallowed=True):
        language = script_language(path)
        if language:
            rows.append((f"scripts/{rel}", path, language))
    return sorted(rows, key=lambda item: item[0])


def _clean_ref(value: str) -> Optional[str]:
    value = value.strip().strip("'\"").rstrip(".,;:!?")
    if value in TOP_FILES:
        return value
    if not any(value.startswith(prefix) for prefix in RESOURCE_PREFIXES):
        return None
    if value in RESOURCE_PREFIXES or "..." in value or any(ch in value for ch in "{}<>"):
        return None
    return value


def _tokens(text: str) -> Set[str]:
    refs: Set[str] = set()
    for match in re.finditer(
        r"(?P<q>['\"])(?P<p>(?:scripts|references|assets|agents)/.+?)(?P=q)", text
    ):
        cleaned = _clean_ref(match.group("p"))
        if cleaned:
            refs.add(cleaned)
    for match in re.finditer(
        r"(?<![\w./-])((?:scripts|references|assets|agents)/[^\s`<>\"'()\[\]{}]+)", text
    ):
        cleaned = _clean_ref(match.group(1))
        if cleaned:
            refs.add(cleaned)
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        words = []
    for word in words:
        cleaned = _clean_ref(word)
        if cleaned:
            refs.add(cleaned)
    return refs


def extract_markdown_paths(text: str) -> Set[str]:
    refs: Set[str] = set()
    link_re = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\n]+)\s*\)")
    for match in link_re.finditer(text):
        raw = match.group(1).strip()
        if raw.startswith("<") and raw.endswith(">"):
            destination = raw[1:-1]
        else:
            try:
                destination = shlex.split(raw, posix=True)[0]
            except (ValueError, IndexError):
                destination = raw.split()[0] if raw.split() else ""
        cleaned = _clean_ref(unquote(destination).split("#", 1)[0])
        if cleaned:
            refs.add(cleaned)
    code_re = re.compile(r"(?P<f>`+)(?P<body>.*?)(?P=f)", re.DOTALL)
    for match in code_re.finditer(text):
        body = match.group("body")
        refs.update(_tokens(body))
        whole = _clean_ref(body.strip())
        if whole and not whole.startswith("scripts/"):
            refs.add(whole)
    masked = code_re.sub("", link_re.sub("", text))
    refs.update(_tokens(masked))
    return refs


def local_script_reference_set(text: str) -> Set[str]:
    return {ref for ref in extract_markdown_paths(text) if ref.startswith("scripts/")}


def parse_self_test_command(command: str, skill_dir: Path) -> ParsedSelfTest:
    if not isinstance(command, str) or not command.strip():
        raise CommandPolicyError("metadata.self_test must be a non-empty string")
    if SHELL_META_RE.search(command):
        raise CommandPolicyError("self-test command contains shell metacharacters")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise CommandPolicyError(f"invalid quoting: {exc}") from exc
    if not argv:
        raise CommandPolicyError("self-test command is empty")
    first = argv[0]
    direct = first.startswith("scripts/") or first.startswith("./scripts/")
    if direct:
        rel, target = resolve_inside(skill_dir, first, must_exist=True, require_file=True)
        language = script_language(target)
        if not language or language == "unknown":
            raise CommandPolicyError(f"self-test target is not a recognized script: {rel}")
        if os.name != "nt" and not os.access(target, os.X_OK):
            raise CommandPolicyError(f"direct self-test script is not executable: {rel}")
        return ParsedSelfTest(command, tuple(argv), f"/skill/{rel}", rel, language, True)

    if "/" in first or "\\" in first:
        raise CommandPolicyError("interpreter must be an allowlisted command name")
    name = Path(first).name.lower()
    if PYTHON_RE.fullmatch(name):
        language, forbidden = "python", {"-c", "-m", "--command"}
    elif name in {"bash", "sh"}:
        language, forbidden = "shell", {"-c", "--command"}
    elif name in {"node", "nodejs"}:
        language, forbidden = "javascript", {"-e", "--eval", "-p", "--print", "-r", "--require"}
    else:
        raise CommandPolicyError("self-test must use python, bash/sh, node, or a direct local script")
    executable = shutil.which(first)
    if not executable:
        raise CommandPolicyError(f"declared interpreter is unavailable: {first}")
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        if argv[index] in forbidden or any(
            argv[index].startswith(item + "=") for item in forbidden if item.startswith("--")
        ):
            raise CommandPolicyError(f"inline/module execution is not allowed: {argv[index]}")
        index += 1
    if index >= len(argv):
        raise CommandPolicyError("self-test command does not reference a local script file")
    token = argv[index]
    if not (token.startswith("scripts/") or token.startswith("./scripts/")):
        raise CommandPolicyError("self-test target must be under scripts/")
    rel, target = resolve_inside(skill_dir, token, must_exist=True, require_file=True)
    target_lang = script_language(target)
    allowed = {
        "python": {"python"}, "shell": {"shell"},
        "javascript": {"javascript", "typescript"},
    }[language]
    if target_lang not in allowed:
        raise CommandPolicyError(
            f"interpreter '{first}' does not match '{rel}' ({target_lang or 'unknown'})"
        )
    return ParsedSelfTest(command, tuple(argv), executable, rel, language, False)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def self_test_failure_evidence(path: Path, language: str) -> List[str]:
    text = read_utf8(path)
    evidence: Set[str] = set()
    if language == "python":
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                evidence.add("assert")
            elif isinstance(node, ast.Raise):
                evidence.add("raise")
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name.endswith((".fail", ".raises", ".assertEqual", ".assertTrue", ".assertFalse")):
                    evidence.add(name.rsplit(".", 1)[-1])
                if name in {"pytest.main", "unittest.main"}:
                    evidence.add(name)
                if name in {"sys.exit", "exit"}:
                    if not node.args or not (
                        isinstance(node.args[0], ast.Constant)
                        and node.args[0].value in {0, None, False}
                    ):
                        evidence.add("nonzero-capable exit")
                if any(
                    kw.arg == "check" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                    for kw in node.keywords
                ):
                    evidence.add("subprocess check=True")
    elif language == "shell":
        patterns = {
            "errexit": r"(?m)^\s*set\s+-[^\n]*e",
            "nonzero exit": r"\bexit\s+[1-9][0-9]*\b",
            "false": r"(?:^|[;&|\s])false(?:$|[;&|\s])",
            "test": r"(?:^|[;&|\s])(?:test\s|\[\[?\s)",
            "negation": r"(?:^|[;&|\s])!\s+",
        }
        for label, pattern in patterns.items():
            if re.search(pattern, text):
                evidence.add(label)
    elif language in {"javascript", "typescript"}:
        for label, pattern in {
            "throw": r"\bthrow\b", "assert": r"\b(?:assert|console\.assert)\s*\(",
            "nonzero exit": r"\bprocess\.exit\s*\(\s*[1-9]",
            "reject": r"\bPromise\.reject\s*\(",
        }.items():
            if re.search(pattern, text):
                evidence.add(label)
    return sorted(evidence)
