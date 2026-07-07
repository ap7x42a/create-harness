#!/usr/bin/env python3
"""Strict static validator for harnessed Agent Skills.

Target skills are treated as data. This validator does not import target modules,
invoke target-declared commands, or execute target self-tests. It makes no claim
that untrusted code can be executed safely.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

sys.dont_write_bytecode = True

from skill_utils import (
    CommandPolicyError,
    DependencyError,
    FrontmatterDocument,
    FrontmatterError,
    PathPolicyError,
    SkillToolError,
    discover_scripts,
    extract_markdown_paths,
    find_disallowed_package_artifacts,
    find_symlinks,
    iter_regular_files,
    local_script_reference_set,
    parse_self_test_command,
    read_skill_document,
    read_utf8,
    resolve_inside,
    script_language,
    self_test_failure_evidence,
)
from write_manifest import check_manifest

ALLOWED_FRONTMATTER_KEYS = {
    "name", "description", "license", "compatibility", "metadata", "allowed-tools"
}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SUBPROCESS_TIMEOUT = 30
RECEIPT_ONLY_NAMES = {
    "self_test.py", "self-test.py", "selftest.py",
    "self_test.sh", "self-test.sh", "selftest.sh",
    "self_test.js", "self-test.js", "selftest.js",
    "self_test.mjs", "self-test.mjs", "selftest.mjs",
    "self_test.cjs", "self-test.cjs", "selftest.cjs",
}
CONTENT_DIRS = ("references", "assets", "agents")
IGNORABLE_CONTENT_NAMES = {"README", "README.md", ".gitkeep", ".keep"}


class Report:
    def __init__(self, skill_dir: Path) -> None:
        self.skill_dir = str(skill_dir)
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.checks: Dict[str, object] = {}

    def err(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def as_dict(self) -> Dict[str, object]:
        return {
            "result": "pass" if not self.errors else "fail",
            "skill_dir": self.skill_dir,
            "mode": "static",
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checks,
        }


def _safe_check(rep: Report, label: str, function, *args):
    try:
        return function(*args)
    except (DependencyError, FrontmatterError, CommandPolicyError,
            PathPolicyError, SkillToolError, OSError, UnicodeError,
            subprocess.SubprocessError, ValueError, TypeError) as exc:
        rep.err(f"{label}: {exc}")
    except Exception as exc:  # A malformed target must never crash the validator.
        rep.err(f"{label}: unexpected validation failure ({type(exc).__name__}: {exc})")
    return None


def check_tree(skill_dir: Path, rep: Report) -> None:
    symlinks = find_symlinks(skill_dir, skip_disallowed=False)
    for rel in symlinks:
        rep.err(f"unsafe tree: symlink is not allowed: {rel}")

    disallowed = find_disallowed_package_artifacts(skill_dir)
    for rel in disallowed:
        rep.err(f"package artifact is not allowed: {rel}")

    file_count = 0
    try:
        # Skip already-reported cache/VCS directories so their contents do not
        # create redundant diagnostics, but still reject special file types.
        file_count = sum(1 for _ in iter_regular_files(
            skill_dir, skip_disallowed=bool(disallowed)
        ))
    except PathPolicyError as exc:
        rep.err(f"unsafe tree: {exc}")
    rep.checks["tree"] = {
        "files": file_count,
        "symlinks": len(symlinks),
        "disallowed_artifacts": len(disallowed),
    }


def _string_field(
    fm: Mapping[str, object], key: str, rep: Report, *, required: bool = False,
    max_length: Optional[int] = None, nonempty: bool = True,
) -> Optional[str]:
    if key not in fm:
        if required:
            rep.err(f"frontmatter missing '{key}'")
        return None
    value = fm[key]
    if not isinstance(value, str):
        rep.err(f"frontmatter '{key}' must be a string")
        return None
    if nonempty and not value.strip():
        rep.err(f"frontmatter '{key}' must not be empty")
    if max_length is not None and len(value) > max_length:
        rep.err(f"frontmatter '{key}' is too long ({len(value)} > {max_length})")
    return value


def check_frontmatter(skill_dir: Path, rep: Report) -> Optional[FrontmatterDocument]:
    skill_paths = []
    for rel, _path in iter_regular_files(skill_dir, skip_disallowed=True):
        if Path(rel).name == "SKILL.md":
            skill_paths.append(rel)
    if "SKILL.md" not in skill_paths:
        rep.err("SKILL.md not found at the skill root")
        rep.checks["frontmatter"] = {"skill_md_count": len(skill_paths)}
        return None
    if len(skill_paths) != 1:
        rep.err(
            "a packaged skill must contain exactly one SKILL.md; found: "
            + ", ".join(sorted(skill_paths))
        )

    document = read_skill_document(skill_dir)
    fm = document.data
    extras = sorted(set(fm) - ALLOWED_FRONTMATTER_KEYS)
    if extras:
        rep.err(
            "unexpected frontmatter key(s): " + ", ".join(extras)
            + "; allowed keys are: " + ", ".join(sorted(ALLOWED_FRONTMATTER_KEYS))
        )

    name = _string_field(fm, "name", rep, required=True, max_length=64)
    if name is not None:
        if not NAME_RE.fullmatch(name):
            rep.err(
                f"frontmatter name '{name}' must use lowercase letters, digits, and "
                "single interior hyphens"
            )
        # The consuming symlink strips a trailing ".skill" suffix from the
        # directory name, so a ".skill"-suffixed source dir matches when the
        # frontmatter name equals the dir name with that suffix removed.
        expected = skill_dir.name
        if expected.endswith(".skill"):
            expected = expected[: -len(".skill")]
        if name != expected:
            rep.err(
                f"frontmatter name '{name}' must match parent directory '{skill_dir.name}'"
            )

    description = _string_field(
        fm, "description", rep, required=True, max_length=1024
    )
    if description is not None and ("<" in description or ">" in description):
        rep.err("frontmatter description must not contain angle brackets")

    compatibility = _string_field(
        fm, "compatibility", rep, max_length=500
    ) if "compatibility" in fm else None
    _string_field(fm, "license", rep) if "license" in fm else None
    _string_field(fm, "allowed-tools", rep) if "allowed-tools" in fm else None

    metadata = fm.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            rep.err("frontmatter 'metadata' must be a mapping of string keys to string values")
        else:
            for key, value in metadata.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    rep.err("frontmatter 'metadata' must map string keys to string values")
                    break

    if not document.body.strip():
        rep.warn("SKILL.md has no instruction body")
    line_count = len((skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines())
    if line_count > 500:
        rep.warn(f"SKILL.md is {line_count} lines; consider progressive disclosure")

    rep.checks["frontmatter"] = {
        "skill_md_count": len(skill_paths),
        "name": name,
        "description_length": len(description) if isinstance(description, str) else None,
        "compatibility_length": len(compatibility) if isinstance(compatibility, str) else None,
        "skill_md_lines": line_count,
    }
    return document


def _all_markdown_script_refs(skill_dir: Path) -> Set[str]:
    refs: Set[str] = set()
    for rel, path in iter_regular_files(skill_dir, skip_disallowed=True):
        if Path(rel).suffix.lower() == ".md":
            try:
                refs.update(local_script_reference_set(read_utf8(path)))
            except SkillToolError:
                continue
    return refs


def check_paths(
    skill_dir: Path, document: Optional[FrontmatterDocument], rep: Report
) -> Set[str]:
    if document is None:
        rep.checks["paths"] = {"referenced": 0, "missing_or_unsafe": 0}
        return set()

    refs = sorted(extract_markdown_paths(document.body))
    failures = 0
    for raw in refs:
        try:
            resolve_inside(
                skill_dir, raw, must_exist=True, require_file=True,
                decode_markdown_url=True,
            )
        except PathPolicyError as exc:
            failures += 1
            kind = "dangling reference" if "does not exist" in str(exc) else "unsafe reference"
            rep.err(f"{kind}: SKILL.md mentions '{raw}' ({exc})")

    all_live_scripts = _all_markdown_script_refs(skill_dir)
    on_disk = {rel for rel, _path, _lang in discover_scripts(skill_dir)}
    for rel in sorted(on_disk - all_live_scripts):
        rep.warn(f"dead harness: '{rel}' is not referenced in any Markdown file")

    rep.checks["paths"] = {
        "referenced": len(refs),
        "missing_or_unsafe": failures,
        "script_references": len(all_live_scripts),
        "unreferenced_scripts": len(on_disk - all_live_scripts),
    }
    return local_script_reference_set(document.body)


def _run_syntax_check(command: Sequence[str]) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            list(command), shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=SUBPROCESS_TIMEOUT,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "syntax check timed out"
    detail = (result.stderr or result.stdout).strip()
    if len(detail) > 2000:
        detail = detail[:2000] + "…"
    return result.returncode, detail


def check_scripts(skill_dir: Path, rep: Report) -> List[Tuple[str, Path, str]]:
    scripts = discover_scripts(skill_dir)
    counts: Dict[str, int] = {}
    failed = 0

    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        known_paths = {path for _rel, path, _language in scripts}
        for rel, path in iter_regular_files(scripts_dir, skip_disallowed=True):
            if path in known_paths:
                continue
            if os.name != "nt" and os.access(path, os.X_OK):
                rep.err(
                    f"unknown executable: scripts/{rel} has no supported extension or shebang"
                )
                failed += 1

    for rel, path, language in scripts:
        counts[language] = counts.get(language, 0) + 1
        if language == "python":
            try:
                compile(read_utf8(path), str(path), "exec")
            except SyntaxError as exc:
                rep.err(
                    f"python does not compile: {rel} ({exc.msg} at line {exc.lineno})"
                )
                failed += 1
            except SkillToolError as exc:
                rep.err(f"python cannot be checked: {rel} ({exc})")
                failed += 1
        elif language == "shell":
            bash = shutil.which("bash")
            if not bash:
                rep.err(f"shell script cannot be verified because bash is unavailable: {rel}")
                failed += 1
                continue
            code, detail = _run_syntax_check([bash, "-n", str(path)])
            if code != 0:
                rep.err(f"shell does not parse: {rel} ({detail or 'parse error'})")
                failed += 1
        elif language == "javascript":
            node = shutil.which("node") or shutil.which("nodejs")
            if not node:
                rep.err(f"JavaScript cannot be verified because Node.js is unavailable: {rel}")
                failed += 1
                continue
            code, detail = _run_syntax_check([node, "--check", str(path)])
            if code != 0:
                rep.err(f"JavaScript does not parse: {rel} ({detail or 'parse error'})")
                failed += 1
        elif language == "typescript":
            rep.err(
                f"TypeScript is not accepted without a trusted project-specific build check: {rel}"
            )
            failed += 1
        else:
            rep.err(f"unrecognized script language: {rel}")
            failed += 1

    rep.checks["scripts"] = {
        "total": len(scripts), "by_language": counts, "failed": failed,
    }
    return scripts


def check_self_test_static(
    skill_dir: Path, document: Optional[FrontmatterDocument], rep: Report
):
    if document is None:
        rep.checks["self_test"] = {"declared": False, "ran": False}
        return None
    metadata = document.data.get("metadata")
    if not isinstance(metadata, dict) or "self_test" not in metadata:
        rep.err(
            "frontmatter metadata.self_test is required and must name a local test script"
        )
        rep.checks["self_test"] = {"declared": False, "ran": False}
        return None
    command = metadata.get("self_test")
    try:
        parsed = parse_self_test_command(command, skill_dir)  # type: ignore[arg-type]
    except (CommandPolicyError, PathPolicyError, OSError) as exc:
        rep.err(f"invalid metadata.self_test: {exc}")
        rep.checks["self_test"] = {
            "declared": True, "valid_command": False, "ran": False,
        }
        return None

    target = skill_dir / parsed.target_rel
    evidence = self_test_failure_evidence(target, parsed.language)
    if not evidence:
        rep.err(
            f"self-test '{parsed.target_rel}' has no visible failure assertion or nonzero-capable exit"
        )
    rep.checks["self_test"] = {
        "declared": True,
        "valid_command": True,
        "target": parsed.target_rel,
        "language": parsed.language,
        "failure_evidence": evidence,
        "ran": False,
    }
    return parsed


def _has_meaningful_content(skill_dir: Path, dirname: str) -> bool:
    root = skill_dir / dirname
    if not root.is_dir():
        return False
    for rel, _path in iter_regular_files(root, skip_disallowed=True):
        if Path(rel).name not in IGNORABLE_CONTENT_NAMES:
            return True
    return False


def check_harness_present(
    skill_dir: Path,
    scripts: List[Tuple[str, Path, str]],
    self_test,
    live_script_refs: Set[str],
    rep: Report,
) -> None:
    functional_scripts: List[str] = []
    self_test_rel = self_test.target_rel if self_test is not None else None
    for rel, path, _language in scripts:
        is_receipt_only = rel == self_test_rel and (
            path.name.lower() in RECEIPT_ONLY_NAMES or rel not in live_script_refs
        )
        if not is_receipt_only:
            functional_scripts.append(rel)

    content_dirs = [name for name in CONTENT_DIRS if _has_meaningful_content(skill_dir, name)]
    if not functional_scripts and not content_dirs:
        rep.err(
            "no functional harness content: add a reusable script, reference, asset, or agent; "
            "a receipt-only self-test does not count"
        )
    rep.checks["harness"] = {
        "functional_scripts": functional_scripts,
        "content_directories": content_dirs,
        "present": bool(functional_scripts or content_dirs),
    }


def check_drift_manifest(skill_dir: Path, rep: Report) -> None:
    ok, results = check_manifest(skill_dir)
    problems = [(path, status) for path, status in results if status != "OK"]
    if not ok:
        if not problems:
            rep.err("drift manifest failed verification")
        for path, status in problems:
            rep.err(f"drift manifest: {status}: {path}")
    rep.checks["manifest"] = {
        "verified": ok,
        "ok_entries": sum(status == "OK" for _path, status in results),
        "problems": [
            {"path": path, "status": status} for path, status in problems
        ],
        "purpose": "drift detection relative to a trusted manifest; not publisher provenance",
    }



def validate(skill_dir: Path) -> Report:
    rep = Report(skill_dir)
    try:
        skill_dir = skill_dir.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        rep.err(f"invalid skill directory: {exc}")
        return rep
    if not skill_dir.is_dir():
        rep.err(f"not a directory: {skill_dir}")
        return rep

    _safe_check(rep, "tree", check_tree, skill_dir, rep)
    document = _safe_check(rep, "frontmatter", check_frontmatter, skill_dir, rep)
    live_refs = _safe_check(rep, "paths", check_paths, skill_dir, document, rep) or set()
    scripts = _safe_check(rep, "scripts", check_scripts, skill_dir, rep) or []
    parsed = _safe_check(rep, "self-test", check_self_test_static, skill_dir, document, rep)
    _safe_check(
        rep, "harness", check_harness_present,
        skill_dir, scripts, parsed, live_refs, rep,
    )
    _safe_check(rep, "manifest", check_drift_manifest, skill_dir, rep)

    return rep


def render(rep: Report) -> str:
    status = "PASS" if not rep.errors else "FAIL"
    lines = [f"RESULT: {status}", "MODE: static"]
    for message in rep.errors:
        lines.append(f"ERROR: {message}")
    for message in rep.warnings:
        lines.append(f"WARN: {message}")
    if not rep.errors:
        lines.append("All required checks passed.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Statically validate a harnessed Agent Skill. Target-declared commands "
            "and self-tests are never executed."
        )
    )
    parser.add_argument("skill_dir")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    args = parser.parse_args()
    rep = validate(Path(args.skill_dir))
    print(json.dumps(rep.as_dict(), indent=2) if args.json else render(rep))
    raise SystemExit(0 if not rep.errors else 1)


if __name__ == "__main__":
    main()
