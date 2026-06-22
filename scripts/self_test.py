#!/usr/bin/env python3
"""Regression suite for the create-harness toolchain.

The suite builds disposable skills and verifies positive behavior, malformed-input
handling, path policy, receipt integrity, and the static-only execution boundary.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Dict, Optional

sys.dont_write_bytecode = True

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit_skill
import validate_harness
import write_manifest
from skill_utils import PathPolicyError, read_skill_document

FAILURES = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(label)
        if detail:
            print("     " + detail.replace("\n", "\n     ")[:3000])


def write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    if executable:
        path.chmod(0o755)


def manifest(skill_dir: Path) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        result = write_manifest.write_manifest(skill_dir)
    if result != 0:
        raise RuntimeError("manifest writer failed")


def errors(report: validate_harness.Report) -> str:
    return " | ".join(report.errors).lower()


def make_skill(
    root: Path,
    name: str,
    *,
    body: str = "Run `scripts/tool.py`.\n",
    command: str = "python3 scripts/self_test.py",
    tool: Optional[str] = None,
    self_test: Optional[str] = None,
    extra_files: Optional[Dict[str, str]] = None,
    description_block: Optional[str] = None,
    frontmatter_extra: str = "compatibility: Requires Python 3.8+.\n",
    with_manifest: bool = True,
) -> Path:
    skill = root / name
    description = description_block or (
        ">-\n  A disposable regression fixture used to verify strict static validation, "
        "path safety, receipt discipline, and clean failure behavior."
    )
    command_yaml = json.dumps(command)
    skill_text = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{frontmatter_extra.rstrip()}\n"
        "metadata:\n"
        f"  self_test: {command_yaml}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{textwrap.dedent(body).strip()}\n"
    )
    write(skill / "SKILL.md", skill_text)
    write(skill / "scripts" / "tool.py", tool or """
        #!/usr/bin/env python3
        def add(a, b):
            return a + b

        if __name__ == "__main__":
            print(add(2, 3))
    """, executable=True)
    write(skill / "scripts" / "self_test.py", self_test or """
        #!/usr/bin/env python3
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import tool
        assert tool.add(2, 3) == 5
        print("fixture self-test: ok")
    """, executable=True)
    write(skill / "BUILD_RECEIPT.md", "# Build receipt\n\nFixture receipt.\n")
    if extra_files:
        for rel, content in extra_files.items():
            write(skill / rel, content)
    if with_manifest:
        manifest(skill)
    return skill


def make_markdown_only(root: Path) -> Path:
    skill = root / "markdown-only"
    write(skill / "SKILL.md", """
        ---
        name: markdown-only
        description: A prose-only fixture containing repeated deterministic code that should be extracted into a reusable harness.
        ---

        # Markdown only

        Every invocation, write a Python script to parse and validate the input:

        ```python
        import csv
        import sys
        rows = list(csv.reader(open(sys.argv[1])))
        total = 0
        for row in rows:
            if len(row) >= 3:
                total += int(row[2])
        print(total)
        if total < 0:
            raise SystemExit(1)
        ```
    """)
    return skill


def subprocess_json(args, *, timeout: int = 30):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, *map(str, args)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout, env=env, check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    return result, payload


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="create-harness-tests-") as temp:
        root = Path(temp)

        # Audit correctness: prose-only, extraction, and fake-harness bypasses.
        md = make_markdown_only(root)
        report = audit_skill.audit(str(md))
        check(report.get("state") == "markdown-only",
              "audit classifies a prose-only skill as markdown-only")
        check(bool(report.get("extraction_candidates", {}).get("scripts")),
              "audit finds deterministic code extraction candidates")

        fake = root / "fake-harness"
        write(fake / "SKILL.md", """
            ---
            name: fake-harness
            description: A fake harness containing only receipt-shaped files and no functional resource.
            metadata:
              self_test: "true"
            ---
            # Fake
        """)
        write(fake / "scripts" / "README.md", "No executable harness.\n")
        write(fake / "scripts" / "__pycache__" / "junk.pyc", "not bytecode\n")
        write(fake / "BUILD_RECEIPT.md", "# Receipt\n")
        report = audit_skill.audit(str(fake))
        check(report.get("state") == "markdown-only",
              "audit does not mistake README/cache/one receipt for a harness")
        check(report.get("inventory", {}).get("scripts", {}).get("file_count") == 0,
              "audit excludes cache and documentation files from script inventory")

        # Positive fixture, including folded YAML and a Unicode path with spaces.
        good = make_skill(
            root, "good",
            body=(
                "Run `scripts/tool.py`. Read [the notes](<references/Notes ü.md>). "
                "The regression gate is `scripts/self_test.py`.\n"
            ),
            extra_files={"references/Notes ü.md": "# Notes\n\nEdge cases.\n"},
        )
        good_report = validate_harness.validate(good)
        check(not good_report.errors,
              "strict static validation passes a sound harness",
              "\n".join(good_report.errors))
        check(good_report.checks.get("self_test", {}).get("ran") is False,
              "static validation does not execute the target self-test")
        doc = read_skill_document(good)
        check(isinstance(doc.data.get("description"), str)
              and doc.data.get("description") != ">-",
              "strict YAML parser handles folded descriptions correctly")
        check(audit_skill.audit(str(good)).get("state") == "harnessed",
              "audit classifies a complete, verified harness as harnessed")

        # Command injection and no-op receipt bypasses.
        marker = root / "VALIDATOR_RCE_MARKER"
        injected = make_skill(
            root, "injected",
            command=f"python3 scripts/self_test.py ; printf exploited > {marker}",
        )
        injected_report = validate_harness.validate(injected)
        check("shell metacharacters" in errors(injected_report),
              "self-test shell injection is rejected statically")
        check(not marker.exists(), "validator never executes an injected command")

        noop = make_skill(
            root, "noop",
            self_test="print('always passes')\n",
        )
        noop_report = validate_harness.validate(noop)
        check("no visible failure" in errors(noop_report),
              "print-only no-op self-test is rejected")

        receipt_only = root / "receipt-only"
        write(receipt_only / "SKILL.md", """
            ---
            name: receipt-only
            description: A fixture containing a credible self-test but no functional harness content.
            metadata:
              self_test: "python3 scripts/self_test.py"
            ---
            # Receipt only
        """)
        write(receipt_only / "scripts" / "self_test.py", "assert 2 + 2 == 4\n")
        write(receipt_only / "BUILD_RECEIPT.md", "# Receipt\n")
        manifest(receipt_only)
        receipt_report = validate_harness.validate(receipt_only)
        check("no functional harness content" in errors(receipt_report),
              "receipt-only self-test does not count as a harness")

        prose_true = root / "prose-true"
        write(prose_true / "SKILL.md", """
            ---
            name: prose-true
            description: A prose-only skill attempting to use the true command as a fake self-test.
            metadata:
              self_test: "true"
            ---
            # Prose
        """)
        write(prose_true / "BUILD_RECEIPT.md", "# Receipt\n")
        manifest(prose_true)
        true_report = validate_harness.validate(prose_true)
        check("must use python" in errors(true_report)
              and "no functional harness content" in errors(true_report),
              "self_test true and prose-only content cannot pass")

        missing_test = make_skill(root, "missing-test", with_manifest=False)
        (missing_test / "scripts" / "self_test.py").unlink()
        manifest(missing_test)
        missing_report = validate_harness.validate(missing_test)
        check("path does not exist" in errors(missing_report),
              "missing self-test target is a hard failure")

        inline = make_skill(
            root, "inline",
            command="python3 -c 'raise SystemExit(1)'",
        )
        inline_report = validate_harness.validate(inline)
        check("inline/module execution is not allowed" in errors(inline_report),
              "inline interpreter execution is rejected")

        # PyYAML is explicit and malformed data fails cleanly.
        result, payload = subprocess_json([
            "-S", SCRIPTS / "validate_harness.py", good, "--json"
        ])
        combined = (result.stdout + result.stderr).lower()
        check(result.returncode == 1 and "pyyaml is required" in combined
              and "traceback" not in combined,
              "missing PyYAML produces a clear fail-closed diagnostic")

        malformed = root / "malformed"
        write(malformed / "SKILL.md", "---\nname: malformed\ndescription: [broken\n---\n# Bad\n")
        malformed_report = validate_harness.validate(malformed)
        check(bool(malformed_report.errors)
              and "unexpected validation failure" not in errors(malformed_report),
              "malformed YAML becomes a structured validation failure")

        duplicate_yaml = root / "duplicate-yaml"
        write(duplicate_yaml / "SKILL.md", """
            ---
            name: duplicate-yaml
            name: duplicate-yaml
            description: Duplicate keys must not be accepted by the strict YAML loader.
            ---
            # Bad
        """)
        duplicate_report = validate_harness.validate(duplicate_yaml)
        check("duplicate key" in errors(duplicate_report),
              "duplicate YAML keys are rejected")

        list_name = root / "list-name"
        write(list_name / "SKILL.md", """
            ---
            name: [bad]
            description: A malformed type fixture that must fail cleanly instead of raising an attribute error.
            ---
            # Bad type
        """)
        list_report = validate_harness.validate(list_name)
        check("name' must be a string" in errors(list_report),
              "non-string name fails cleanly")

        mismatch = make_skill(root, "directory-name", with_manifest=False)
        mismatch_text = (mismatch / "SKILL.md").read_text(encoding="utf-8")
        (mismatch / "SKILL.md").write_text(
            mismatch_text.replace("name: directory-name", "name: different-name"),
            encoding="utf-8",
        )
        manifest(mismatch)
        mismatch_report = validate_harness.validate(mismatch)
        check("must match parent directory" in errors(mismatch_report),
              "frontmatter name/directory mismatch is a hard failure")

        bad_types = root / "bad-types"
        write(bad_types / "SKILL.md", """
            ---
            name: bad-types
            description: Frontmatter type checks cover compatibility, allowed-tools, and metadata.
            compatibility: [linux]
            allowed-tools: [Read]
            metadata:
              self_test: 7
            ---
            # Types
        """)
        types_report = validate_harness.validate(bad_types)
        type_errors = errors(types_report)
        check("compatibility' must be a string" in type_errors
              and "allowed-tools' must be a string" in type_errors
              and "string keys to string values" in type_errors,
              "current frontmatter field types are enforced")

        # Resource and manifest containment/exactness.
        outside = root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        traversal = make_skill(
            root, "traversal",
            body="Run `scripts/../../outside.txt` and `scripts/tool.py`.\n",
        )
        traversal_report = validate_harness.validate(traversal)
        check("unsafe reference" in errors(traversal_report),
              "SKILL.md traversal cannot escape the skill root")

        extensionless_missing = make_skill(
            root, "extensionless-missing",
            body="Run `scripts/missing` and `scripts/tool.py`.\n",
        )
        extensionless_report = validate_harness.validate(extensionless_missing)
        check("scripts/missing" in errors(extensionless_report),
              "dangling extensionless resource paths are detected")

        manifest_traversal = make_skill(root, "manifest-traversal")
        with (manifest_traversal / "SHA256SUMS.txt").open("a", encoding="utf-8") as handle:
            handle.write("0" * 64 + "  ./../outside.txt\n")
        ok, rows = write_manifest.check_manifest(manifest_traversal)
        check(not ok and any("UNSAFE_PATH" in status for _path, status in rows),
              "manifest traversal is rejected")

        duplicate_manifest = make_skill(root, "duplicate-manifest")
        manifest_path = duplicate_manifest / "SHA256SUMS.txt"
        first = manifest_path.read_text(encoding="utf-8").splitlines()[0]
        with manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(first + "\n")
        ok, rows = write_manifest.check_manifest(duplicate_manifest)
        check(not ok and any(status == "DUPLICATE" for _path, status in rows),
              "duplicate manifest entries are rejected")

        directory_entry = make_skill(root, "directory-entry")
        with (directory_entry / "SHA256SUMS.txt").open("a", encoding="utf-8") as handle:
            handle.write("0" * 64 + "  ./scripts\n")
        ok, rows = write_manifest.check_manifest(directory_entry)
        check(not ok and any("UNSAFE_PATH" in status for _path, status in rows),
              "manifest directory entries fail cleanly")

        extra_entry = make_skill(root, "extra-entry")
        digest = hashlib.sha256((extra_entry / "SHA256SUMS.txt").read_bytes()).hexdigest()
        with (extra_entry / "SHA256SUMS.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"{digest}  ./SHA256SUMS.txt\n")
        ok, rows = write_manifest.check_manifest(extra_entry)
        check(not ok and any(status == "EXTRA" for _path, status in rows),
              "manifest must match the exact package file set")

        tampered = make_skill(root, "tampered")
        (tampered / "scripts" / "tool.py").write_text("print('changed')\n", encoding="utf-8")
        ok, _rows = write_manifest.check_manifest(tampered)
        check(not ok, "file tampering breaks drift-manifest verification")

        symlinked = make_skill(root, "symlinked")
        link = symlinked / "references" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                write_manifest.write_manifest(symlinked)
        except (PathPolicyError, OSError):
            rejected = True
        else:
            rejected = False
        check(rejected, "manifest writer rejects symlinks")

        missing_skill = root / "missing-skill"
        missing_skill.mkdir()
        missing_skill_report = validate_harness.validate(missing_skill)
        check("skill.md not found" in errors(missing_skill_report),
              "missing SKILL.md is reported without a traceback")

        # Language coverage and fail-closed policies.
        bad_shell = make_skill(root, "bad-shell", body="Run `scripts/tool.sh`.\n")
        (bad_shell / "scripts" / "tool.py").unlink()
        write(bad_shell / "scripts" / "tool.sh", "#!/bin/bash\nif then\n", executable=True)
        manifest(bad_shell)
        shell_report = validate_harness.validate(bad_shell)
        check("shell does not parse" in errors(shell_report),
              "malformed Bash is rejected with bash -n")

        bad_mjs = make_skill(root, "bad-mjs", body="Run `scripts/tool.mjs`.\n")
        (bad_mjs / "scripts" / "tool.py").unlink()
        write(bad_mjs / "scripts" / "tool.mjs", "export const = ;\n")
        manifest(bad_mjs)
        mjs_report = validate_harness.validate(bad_mjs)
        check("javascript does not parse" in errors(mjs_report)
              or "node.js is unavailable" in errors(mjs_report),
              ".mjs is checked or fails closed when Node.js is unavailable")

        extensionless_bad = make_skill(
            root, "extensionless-bad", body="Run `scripts/tool`.\n"
        )
        (extensionless_bad / "scripts" / "tool.py").unlink()
        write(extensionless_bad / "scripts" / "tool", "#!/bin/bash\nif then\n", executable=True)
        manifest(extensionless_bad)
        ext_bad_report = validate_harness.validate(extensionless_bad)
        check("shell does not parse" in errors(ext_bad_report),
              "extensionless shebang scripts receive language syntax checks")

        unknown_exec = make_skill(root, "unknown-exec", body="Run `scripts/tool`.\n")
        (unknown_exec / "scripts" / "tool.py").unlink()
        write(unknown_exec / "scripts" / "tool", "opaque executable\n", executable=True)
        manifest(unknown_exec)
        unknown_report = validate_harness.validate(unknown_exec)
        check("unknown executable" in errors(unknown_report),
              "extensionless executable without a recognized shebang is rejected")

        typescript = make_skill(root, "typescript", body="Run `scripts/tool.ts`.\n")
        (typescript / "scripts" / "tool.py").unlink()
        write(typescript / "scripts" / "tool.ts", "export const value: number = 1;\n")
        manifest(typescript)
        ts_report = validate_harness.validate(typescript)
        check("trusted project-specific build check" in errors(ts_report),
              "TypeScript fails closed instead of receiving a misleading parse claim")

        # The validator exposes no target-code execution path.
        cli_result, _payload = subprocess_json([
            SCRIPTS / "validate_harness.py", good, "--run-self-test"
        ])
        check(
            cli_result.returncode == 2
            and "unrecognized arguments: --run-self-test" in cli_result.stderr,
            "validator exposes no target-code execution option",
            cli_result.stderr,
        )

    print()
    if FAILURES:
        print(f"SELF-TEST FAILED ({len(FAILURES)} of {CHECKS} checks failed)")
        raise SystemExit(1)
    print(f"SELF-TEST PASSED ({CHECKS}/{CHECKS} checks)")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
