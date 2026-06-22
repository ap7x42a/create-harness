#!/usr/bin/env python3
"""Read-only audit for skill-to-harness upgrade candidates.

The target is untrusted data. This tool parses text and filesystem metadata only;
it never imports or executes target code.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.dont_write_bytecode = True

from skill_utils import (
    CommandPolicyError,
    FrontmatterDocument,
    PathPolicyError,
    SkillToolError,
    discover_scripts,
    extract_markdown_paths,
    find_disallowed_package_artifacts,
    iter_regular_files,
    parse_self_test_command,
    read_skill_document,
    read_utf8,
    self_test_failure_evidence,
)
from write_manifest import check_manifest

RECEIPT_ONLY_NAMES = {
    "self_test.py", "self-test.py", "selftest.py",
    "self_test.sh", "self-test.sh", "selftest.sh",
    "self_test.js", "self-test.js", "selftest.js",
    "self_test.mjs", "self-test.mjs", "selftest.mjs",
    "self_test.cjs", "self-test.cjs", "selftest.cjs",
}
CONTENT_DIRS = ("references", "assets", "agents")
IGNORABLE_CONTENT_NAMES = {"README", "README.md", ".gitkeep", ".keep"}
CODE_LANGS = {
    "python", "py", "bash", "sh", "shell", "javascript", "js",
    "typescript", "ts", "json", "yaml", "yml",
}
SIGNALS = {
    r"\bwrite (?:a |the )?(?:python|bash|shell|javascript|script)\b": "re-created scripts",
    r"\bevery (?:time|invocation|run)\b": "repeated work",
    r"\b(?:parse|validate|normalize|transform|convert|checksum|hash)\b": "deterministic transformation",
    r"\bcopy (?:and )?paste\b": "copy/paste workflow",
    r"\b(?:template|boilerplate|scaffold)\b": "reusable asset",
}


def _meaningful_files(skill_dir: Path, dirname: str) -> List[str]:
    root = skill_dir / dirname
    if not root.is_dir():
        return []
    return [
        f"{dirname}/{rel}" for rel, _path in iter_regular_files(root, skip_disallowed=True)
        if Path(rel).name not in IGNORABLE_CONTENT_NAMES
    ]


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _code_candidates(text: str) -> List[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    pattern = re.compile(r"(?ms)^(`{3,}|~{3,})([^\n]*)\n(.*?)^\1\s*$")
    for match in pattern.finditer(text):
        language = match.group(2).strip().split()[0].lower() if match.group(2).strip() else ""
        code = match.group(3).rstrip("\n")
        lines = code.splitlines()
        deterministic = bool(re.search(
            r"\b(?:import|def |function |for |while |subprocess|json|csv|argparse|exit|assert)\b|\$\{?\w+",
            code,
        ))
        if language in CODE_LANGS and (len(lines) >= 5 or deterministic):
            preview = next((line.strip() for line in lines if line.strip()), "")[:100]
            candidates.append({
                "line": _line_number(text, match.start()),
                "language": language or "plain",
                "lines": len(lines),
                "preview": preview,
            })
    return candidates


def _table_candidates(text: str) -> List[Dict[str, int]]:
    lines = text.splitlines()
    rows: List[Dict[str, int]] = []
    index = 0
    separator = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
    while index + 1 < len(lines):
        if "|" in lines[index] and separator.match(lines[index + 1]):
            start = index
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                index += 1
            count = index - start
            if count >= 7:
                rows.append({"line": start + 1, "rows": count - 2})
        else:
            index += 1
    return rows


def _self_test_status(skill_dir: Path, document: Optional[FrontmatterDocument]):
    status: Dict[str, object] = {
        "declared": False, "valid_command": False,
        "target": None, "failure_evidence": [],
    }
    if document is None:
        return status, None
    metadata = document.data.get("metadata")
    if not isinstance(metadata, dict) or "self_test" not in metadata:
        return status, None
    status["declared"] = True
    try:
        parsed = parse_self_test_command(metadata.get("self_test"), skill_dir)  # type: ignore[arg-type]
        evidence = self_test_failure_evidence(skill_dir / parsed.target_rel, parsed.language)
        status.update({
            "valid_command": True,
            "target": parsed.target_rel,
            "language": parsed.language,
            "failure_evidence": evidence,
            "credible_static_test": bool(evidence),
        })
        return status, parsed
    except (CommandPolicyError, PathPolicyError, SkillToolError, OSError) as exc:
        status["error"] = str(exc)
        return status, None


def audit(path: str) -> Dict[str, object]:
    skill_dir = Path(path)
    try:
        skill_dir = skill_dir.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        return {"skill_dir": str(skill_dir), "state": "invalid", "error": str(exc)}
    if not skill_dir.is_dir():
        return {"skill_dir": str(skill_dir), "state": "invalid", "error": "not a directory"}

    document: Optional[FrontmatterDocument] = None
    frontmatter_error: Optional[str] = None
    try:
        document = read_skill_document(skill_dir)
        text = read_utf8(skill_dir / "SKILL.md")
        body = document.body
    except (SkillToolError, OSError, UnicodeError) as exc:
        frontmatter_error = str(exc)
        skill_path = skill_dir / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8", errors="replace") if skill_path.is_file() else ""
        body = text

    scripts = discover_scripts(skill_dir)
    live_refs: Set[str] = {
        ref for ref in extract_markdown_paths(body) if ref.startswith("scripts/")
    }
    self_test_status, parsed = _self_test_status(skill_dir, document)
    self_test_rel = parsed.target_rel if parsed is not None else None

    functional_scripts: List[str] = []
    for rel, script_path, _language in scripts:
        receipt_only = (
            script_path.name.lower() in RECEIPT_ONLY_NAMES
            or (rel == self_test_rel and rel not in live_refs)
        )
        if not receipt_only:
            functional_scripts.append(rel)

    content: Dict[str, List[str]] = {
        dirname: _meaningful_files(skill_dir, dirname) for dirname in CONTENT_DIRS
    }
    functional_harness = bool(functional_scripts or any(content.values()))

    manifest_ok, manifest_results = check_manifest(skill_dir)
    build_receipt = (skill_dir / "BUILD_RECEIPT.md").is_file()
    self_test_ok = bool(
        self_test_status.get("valid_command")
        and self_test_status.get("failure_evidence")
    )
    receipt_complete = manifest_ok and build_receipt and self_test_ok

    if not functional_harness:
        state = "markdown-only"
    elif receipt_complete:
        state = "harnessed"
    else:
        state = "partially-harnessed"

    unreferenced = sorted(
        rel for rel, _path, _lang in scripts
        if rel not in live_refs and rel != self_test_rel
    )
    latent = []
    for pattern, meaning in SIGNALS.items():
        count = len(re.findall(pattern, body, flags=re.I))
        if count:
            latent.append({"meaning": meaning, "count": count})

    inventory = {
        "scripts": {
            "present": bool(scripts),
            "file_count": len(scripts),
            "functional_count": len(functional_scripts),
            "files": [rel for rel, _path, _lang in scripts],
        }
    }
    for dirname, files in content.items():
        inventory[dirname] = {
            "present": bool(files), "file_count": len(files), "files": files,
        }

    name = document.data.get("name") if document and isinstance(document.data.get("name"), str) else None
    description = document.data.get("description") if document and isinstance(document.data.get("description"), str) else None
    line_count = len(text.splitlines())
    word_count = len(re.findall(r"\S+", body))
    bad_artifacts = find_disallowed_package_artifacts(skill_dir)

    return {
        "skill_dir": str(skill_dir),
        "name": name,
        "description": description,
        "state": state,
        "security": {"target_executed": False, "mode": "read-only static inspection"},
        "skill_md": {
            "lines": line_count,
            "words": word_count,
            "over_500_lines": line_count > 500,
            "frontmatter_error": frontmatter_error,
        },
        "inventory": inventory,
        "functional_harness": {
            "present": functional_harness,
            "scripts": functional_scripts,
            "content_directories": [key for key, files in content.items() if files],
        },
        "receipts": {
            "complete": receipt_complete,
            "SHA256SUMS.txt": (skill_dir / "SHA256SUMS.txt").is_file(),
            "manifest_verified": manifest_ok,
            "manifest_problems": [
                {"path": rel, "status": status}
                for rel, status in manifest_results if status != "OK"
            ],
            "BUILD_RECEIPT.md": build_receipt,
            "self_test": self_test_status,
        },
        "extraction_candidates": {
            "scripts": _code_candidates(body),
            "references_tables": _table_candidates(body),
        },
        "latent_harness_signals": latent,
        "unreferenced_scripts": unreferenced,
        "disallowed_artifacts": bad_artifacts,
    }


def render(report: Dict[str, object]) -> str:
    if report.get("state") == "invalid":
        return f"ERROR: {report.get('error', 'invalid skill')}"
    name = report.get("name") or "(unnamed)"
    lines = [
        f"Skill: {name}  [{report['skill_dir']}]",
        f"State: {str(report['state']).upper()}",
    ]
    md = report["skill_md"]  # type: ignore[index]
    lines.append(
        f"SKILL.md: {md['lines']} lines, {md['words']} words"  # type: ignore[index]
        + ("  <- consider progressive disclosure" if md["over_500_lines"] else "")  # type: ignore[index]
    )
    if md["frontmatter_error"]:  # type: ignore[index]
        lines.append(f"  ! frontmatter: {md['frontmatter_error']}")  # type: ignore[index]

    lines.append("\nCurrent harness:")
    inventory = report["inventory"]  # type: ignore[assignment]
    for dirname, info in inventory.items():  # type: ignore[union-attr]
        lines.append(
            f"  {dirname:11} {'yes' if info['present'] else 'no ':3}  "
            f"({info['file_count']} meaningful/recognized files)"
        )
    receipts = report["receipts"]  # type: ignore[assignment]
    lines.append(
        "  receipts    "
        f"manifest={'valid' if receipts['manifest_verified'] else 'invalid'}  "
        f"self-test={'credible' if receipts['self_test'].get('failure_evidence') else 'missing/weak'}  "
        f"build-receipt={'yes' if receipts['BUILD_RECEIPT.md'] else 'no'}"
    )

    candidates = report["extraction_candidates"]["scripts"]  # type: ignore[index]
    lines.append(f"\nExtraction candidates -> scripts ({len(candidates)})")
    for candidate in candidates:
        lines.append(
            f"  L{candidate['line']:>4}  {candidate['language']:<10} "
            f"{candidate['lines']:>3} lines  | {candidate['preview']}"
        )
    if not candidates:
        lines.append("  (none found)")

    if report["unreferenced_scripts"]:
        lines.append("\nUnreferenced scripts:")
        lines.extend(f"  {rel}" for rel in report["unreferenced_scripts"])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a skill without executing target code.")
    parser.add_argument("skill_dir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = audit(args.skill_dir)
    except Exception as exc:  # CLI should always fail cleanly.
        report = {"skill_dir": args.skill_dir, "state": "invalid", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2) if args.json else render(report))
    raise SystemExit(2 if report.get("state") == "invalid" else 0)


if __name__ == "__main__":
    main()
