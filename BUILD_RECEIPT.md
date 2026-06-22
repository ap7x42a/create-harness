# Build receipt

Validated on 2026-06-22 using Python 3.13.5, GNU Bash 5.2.37, and Node.js
22.16.0 on Linux. Each verified claim below names a check that could have failed.
Claims not measured in this environment remain unverified.

This receipt makes no claim that untrusted code can be executed safely. The
validator is static-only.

| Claim | Instrument | Observed evidence | Status | Caveat |
|---|---|---|---|---|
| All bundled Python files compile | `python3 -m py_compile scripts/*.py` | exit 0 | verified | Compilation does not prove behavior |
| The regression suite passes | `python3 scripts/self_test.py` | 36/36 checks pass, exit 0 | verified | Fixtures cover the named regressions, not every possible hostile input |
| The validator never executes target self-tests | positive fixture plus injected-command fixture in the regression suite | `ran` remains false; no marker file created | verified | Host syntax parsers still process source text |
| Shell-command injection is rejected | target declaration containing command separators and output redirection | declaration rejected before execution | verified | Static policy intentionally rejects some complex but benign commands |
| No-op and receipt-only harness bypasses fail | fixtures using `true`, print-only tests, and self-test-only packages | all rejected | verified | Static failure-evidence detection is heuristic, so review remains necessary |
| YAML parsing is standards-based and fail-closed | folded scalar, duplicate key, malformed YAML, non-string types, and `python -S` fixtures | folded scalar parsed; invalid inputs fail cleanly; missing PyYAML is explicit | verified | Requires PyYAML 6+ at runtime |
| Resource paths stay inside the skill root | traversal, extensionless missing path, and Unicode/space Markdown-link fixtures | traversal and missing path rejected; valid Unicode/space link accepted | verified | Only local resources named by SKILL.md are wiring requirements |
| The manifest is exact and canonical | traversal, duplicate, directory, extra, tamper, and symlink fixtures | all invalid forms rejected | verified | The manifest detects drift, not publisher authenticity |
| Malformed packages do not crash validation | missing SKILL.md, malformed YAML, list-valued name, and directory manifest entry | structured failures, no traceback | verified | Unexpected platform-level failures may still prevent validation |
| Current frontmatter invariants are enforced | name/directory mismatch and compatibility, metadata, and allowed-tools type fixtures | all invalid forms rejected | verified | Future specification changes may require updates |
| Python, Bash, JavaScript, and extensionless scripts are covered | compile, `bash -n`, Node syntax check, shebang, and unknown executable fixtures | malformed inputs rejected | verified | TypeScript intentionally fails closed without a trusted project build check |
| Fake audit signals do not create a harness | scripts README, bytecode cache, one receipt, and `self_test: true` fixture | state remains markdown-only | verified | Audit classification is heuristic and should be reviewed |
| The final package manifest verifies | `python3 scripts/write_manifest.py . --check` | all entries OK | verified | Must be rerun after any file change |
| The final package passes its own static gate | `python3 scripts/validate_harness.py .` | RESULT: PASS | verified | Structural and receipt properties only |
| The validator exposes no target-code execution option | invoke the CLI with the removed `--run-self-test` flag | argparse rejects the flag with exit 2 | verified | Executing reviewed code is outside this package and its claims |
| Harnessing improves a target skill's real task performance | controlled before/after task evaluation | not run for an external target skill | unverified | Must be evaluated per target skill against its preserved baseline |

## Reproduce

```bash
python3 -m py_compile scripts/*.py
python3 scripts/self_test.py
python3 scripts/write_manifest.py .
python3 scripts/write_manifest.py . --check
python3 scripts/validate_harness.py .
python3 scripts/audit_skill.py . --json
```
