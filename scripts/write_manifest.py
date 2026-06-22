#!/usr/bin/env python3
"""Write or verify an exact, canonical SHA256SUMS.txt drift manifest.

The manifest detects drift relative to a trusted manifest. It is not a signature
or proof of publisher identity. Python 3.8+; standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

sys.dont_write_bytecode = True

from skill_utils import (
    MANIFEST_NAME, PathPolicyError, SkillToolError, manifest_files,
    normalize_relative_path, resolve_inside, sha256_file,
)

ENTRY_RE = re.compile(r"^([0-9a-f]{64})  (\./[^\r\n]+)$")
Result = Tuple[str, str]


def _expected(skill_dir: Path) -> Dict[str, Path]:
    return {rel: path for rel, path in manifest_files(skill_dir)}


def write_manifest(skill_dir: Path) -> int:
    skill_dir = skill_dir.resolve(strict=True)
    expected = _expected(skill_dir)
    lines = [f"{sha256_file(path)}  ./{rel}" for rel, path in sorted(expected.items())]
    payload = "\n".join(lines) + ("\n" if lines else "")
    fd, temp_name = tempfile.mkstemp(prefix=".SHA256SUMS.", dir=str(skill_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o644)
        except OSError:
            pass
        os.replace(temp_name, skill_dir / MANIFEST_NAME)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    print(f"wrote {MANIFEST_NAME} covering {len(lines)} files")
    return 0


def check_manifest(skill_dir: Path) -> Tuple[bool, List[Result]]:
    results: List[Result] = []
    try:
        skill_dir = skill_dir.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        return False, [(str(skill_dir), f"INVALID_ROOT:{exc}")]
    manifest = skill_dir / MANIFEST_NAME
    if manifest.is_symlink():
        return False, [(MANIFEST_NAME, "SYMLINK")]
    if not manifest.is_file():
        return False, [(MANIFEST_NAME, "MISSING")]
    try:
        expected = _expected(skill_dir)
    except (SkillToolError, PathPolicyError, OSError) as exc:
        return False, [(str(exc), "UNSAFE_TREE")]
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError) as exc:
        return False, [(MANIFEST_NAME, f"UNREADABLE:{exc}")]

    listed: Dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        match = ENTRY_RE.fullmatch(raw)
        if not match:
            results.append((f"line {number}", "MALFORMED"))
            continue
        digest, raw_path = match.groups()
        try:
            rel = normalize_relative_path(raw_path, require_dot_prefix=True)
            if raw_path != f"./{rel}":
                raise PathPolicyError(f"noncanonical path; expected './{rel}'")
            _, target = resolve_inside(
                skill_dir, raw_path, must_exist=True, require_file=True,
                require_dot_prefix=True,
            )
        except (PathPolicyError, OSError) as exc:
            results.append((f"line {number}: {raw_path}", f"UNSAFE_PATH:{exc}"))
            continue
        if rel in listed:
            results.append((rel, "DUPLICATE"))
            continue
        listed[rel] = digest
        if rel not in expected:
            results.append((rel, "EXTRA"))
            continue
        try:
            actual = sha256_file(target)
        except SkillToolError as exc:
            results.append((rel, f"UNREADABLE:{exc}"))
            continue
        results.append((rel, "OK" if actual == digest else "FAILED"))

    for rel in sorted(set(expected) - set(listed)):
        results.append((rel, "UNTRACKED"))
    ok = all(status == "OK" for _, status in results) and set(listed) == set(expected)
    return ok, results


def main() -> None:
    parser = argparse.ArgumentParser(description="Write or verify SHA256SUMS.txt.")
    parser.add_argument("skill_dir")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    skill_dir = Path(args.skill_dir)
    if not skill_dir.is_dir():
        message = f"not a directory: {skill_dir}"
        print(json.dumps({"result": "error", "error": message}, indent=2) if args.json else f"ERROR: {message}")
        raise SystemExit(2)
    try:
        if not args.check:
            raise SystemExit(write_manifest(skill_dir))
        ok, results = check_manifest(skill_dir)
    except (SkillToolError, PathPolicyError, OSError) as exc:
        print(json.dumps({"result": "fail", "error": str(exc)}, indent=2) if args.json else f"ERROR: {exc}")
        raise SystemExit(1)
    bad = [(path, status) for path, status in results if status != "OK"]
    if args.json:
        print(json.dumps({
            "result": "pass" if ok else "fail",
            "ok_entries": sum(status == "OK" for _, status in results),
            "total_results": len(results),
            "problems": [{"path": path, "status": status} for path, status in bad],
        }, indent=2))
    else:
        for path, status in bad:
            print(f"{status}: {path}")
        print(f"manifest check: {'OK' if ok else 'FAILED'} ({sum(s == 'OK' for _, s in results)}/{len(results)} results OK)")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
