# create-harness

`create-harness` upgrades an existing prose-only or weakly tooled Agent Skill
into a repeatable package with reusable scripts, on-demand references, assets,
strict static validation, an exact drift manifest, and falsifiable regression
tests.

## Execution boundary

The target skill is untrusted data. The audit and validator do not import target
modules, invoke target-declared commands, or execute target self-tests. There is
no execution option. This package does not claim that untrusted code can be run
safely, and a passing report is not authorization to run the target.

The manifest detects package drift relative to a trusted manifest. It is not a
signature and does not prove who published the package.

## Requirements

- Python 3.8+
- PyYAML 6+
- Bash when validating shell scripts
- Node.js when validating JavaScript

Install the Python dependency:

```bash
python3 -m pip install -r requirements.txt
```

## Workflow

```text
preserve baseline
  → static audit
  → triage
  → build reusable resources
  → rewire SKILL.md
  → add a credible self-test and build receipt
  → write the drift manifest
  → static validation
  → independent execution decision outside this package
  → before/after task regression
  → package one top-level skill directory
```

## Commands

```bash
python3 scripts/audit_skill.py path/to/skill
python3 scripts/write_manifest.py path/to/skill
python3 scripts/write_manifest.py path/to/skill --check
python3 scripts/validate_harness.py path/to/skill
python3 scripts/self_test.py
```

## Bundled files

- `scripts/audit_skill.py`: read-only extraction and state audit
- `scripts/validate_harness.py`: strict static gate with no target-code execution path
- `scripts/write_manifest.py`: canonical exact-set manifest writer/verifier
- `scripts/skill_utils.py`: strict YAML, path, script, and command utilities
- `scripts/self_test.py`: hostile-fixture regression suite
- `references/harness-patterns.md`: design and implementation guidance
- `BUILD_RECEIPT.md`: claims, instruments, evidence, and caveats
