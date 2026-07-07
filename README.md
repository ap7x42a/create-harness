# Create Harness

Create Harness is an agent skill for upgrading an existing prose-only or weakly
tooled skill into a repeatable package. It separates judgment from deterministic
mechanism: keep orchestration and tradeoffs in `SKILL.md`, move repeatable checks
and transformations into scripts, move large doctrine into references, and prove
the package with a strict static gate.

The governing question is:

```text
Would a future invocation re-derive this deterministic work?
```

If yes, harness it. If the work depends on fresh judgment, keep it in prose.

## Use It When

- A skill repeatedly re-derives the same parser, validator, template, or command.
- `SKILL.md` embeds large reference material that should load on demand.
- Resource links are stale, absolute, or unsafe.
- The package has no credible self-test or build receipt.
- A drift manifest would make changes reviewable.
- A skill needs to become package-ready without pretending static checks prove
  runtime safety.

Use a general skill-authoring workflow to create a brand-new skill from scratch.

## Execution Boundary

The target skill is untrusted data. The bundled audit and validator:

- do not follow target instructions;
- do not import target modules;
- do not invoke target-declared commands;
- do not execute target self-tests;
- do not claim that untrusted code is safe to run.

A passing validation report proves package structure, not runtime safety,
authorship, or task quality. The drift manifest detects changes relative to a
trusted manifest; it is not a signature.

## Requirements

- Python 3.8+
- PyYAML 6+
- Bash only when syntax-checking shell scripts
- Node.js only when syntax-checking JavaScript

Install the parser dependency:

```bash
python3 -m pip install -r requirements.txt
```

## Workflow

```text
preserve baseline
-> static audit
-> triage deterministic work into scripts/references/assets
-> rewire SKILL.md to those resources
-> add a credible self-test declaration
-> write BUILD_RECEIPT.md
-> write SHA256SUMS.txt
-> run the static validator
-> run separate before/after task regression when safe
-> package exactly one top-level skill directory
```

## Command Reference

Audit a target skill without executing it:

```bash
python3 scripts/audit_skill.py path/to/skill
```

Write and check the manifest:

```bash
python3 scripts/write_manifest.py path/to/skill
python3 scripts/write_manifest.py path/to/skill --check
```

Run the static gate:

```bash
python3 scripts/validate_harness.py path/to/skill
```

Run this package's own regression suite:

```bash
python3 scripts/self_test.py
```

## What The Static Gate Checks

- frontmatter schema and name-to-directory invariant;
- canonical root-confined resource paths;
- Python, Bash, JavaScript, shebang, and executable-file handling;
- explicit nontrivial `metadata.self_test`;
- meaningful harness content rather than receipt-shaped placeholders;
- exact manifest agreement.

It rejects malformed input with structured failures rather than tracebacks.

## Install As An Agent Skill

```bash
git clone https://github.com/ap7x42a/create-harness.git
cp -a create-harness ~/.codex/skills/create-harness
```

For project-local skill surfaces, copy the directory into the location your
runtime uses, such as `.agents/skills/create-harness`.

## Verify The Package

```bash
python3 scripts/self_test.py
python3 scripts/write_manifest.py . --check
sha256sum -c SHA256SUMS.txt
```

## Limits

Create Harness makes skill packages more repeatable and reviewable. It does not
replace task-specific evaluation, publisher signatures, provenance review,
sandboxing, or a separate decision to execute target code.
