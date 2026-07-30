# Experiment 0: Authority Snapshot v1

This paper-side experiment freezes the specification authority already present
in Creator's structured `SkillPlan` / `FunctionItem` data. It neither runs nor
changes Creator, and it does not parse prose or infer authority from filenames,
roles, commands, implementations, or observations.

## Three layers

1. **Authority — frozen specification.** Snapshot v1 contains the skill name,
   the allow-listed file plan fields, and the allow-listed FunctionItem fields.
2. **Repairable Implementation — `ResponsibilityEdges` / `SKILL.md` / scripts.**
   These may implement or connect the authority, but are not authority in v1.
3. **Runtime Observation — E2E trace / argv / stdout / artifact.** Observations
   can provide evidence about an implementation but cannot redefine its spec.

Consequently, **Authority Snapshot v1 != whole Skill state**, and **E2E PASS !=
specification preservation**.

## Schema and determinism

The top-level object has `snapshot_version`, `system_commit`, `skill_name`,
`file_plan`, `function_items`, and `snapshot_hash`. File entries retain only
`path`, `file_type`, `file_kind`, `asset_source`, `required`, and `can_skip`.
Function entries retain only `target_file`, `role`, `purpose`, `inputs`,
`outputs`, `default_values`, `required_capabilities`, and `constraints`.

Nested dictionaries and lists are canonicalized, file entries are sorted by
`path`, and function entries by `target_file`. The SHA-256 digest is computed
over canonical UTF-8 JSON without `snapshot_hash`, using `ensure_ascii=False`,
sorted keys, and compact separators. Extra state—including responsibility
edges, Markdown/script content, commands, ToolPool data, traces, and runtime
output—is ignored.

Run only this experiment's tests with:

```bash
python -m pytest -q experiments/authority_drift/tests/test_authority_snapshot.py
```
