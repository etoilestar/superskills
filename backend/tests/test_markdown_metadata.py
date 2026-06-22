from backend.services.markdown_metadata import (
    apply_frontmatter_patch,
    parse_frontmatter,
    validate_reference_frontmatter,
    validate_skill_frontmatter,
)
from backend.routers.creator import validate_file_contract


def test_skill_frontmatter_allows_minimal_name_description():
    frontmatter, body, has = parse_frontmatter("---\nname: demo\ndescription: Demo skill\n---\n# Body\n")

    assert has is True
    assert body == "# Body\n"
    assert validate_skill_frontmatter(frontmatter) == []


def test_skill_frontmatter_rejects_creator_fields():
    issues = validate_skill_frontmatter({"name": "demo", "description": "Demo", "trigger": "x", "inputs": []})

    assert any("trigger" in issue and "inputs" in issue for issue in issues)


def test_reference_frontmatter_optional_and_rejects_internal_fields():
    assert parse_frontmatter("# Reference\n")[2] is False
    assert validate_reference_frontmatter({"title": "Guide", "description": "Use it"}) == []

    issues = validate_reference_frontmatter({"title": "Guide", "required_capabilities": ["x"]})
    assert any("required_capabilities" in issue for issue in issues)


def test_apply_frontmatter_patch_preserves_body_and_code_blocks():
    original = "---\nname: demo\ndescription: Demo\ntrigger: bad\n---\n# Body\n```json\n{\"trigger\": true}\n```\n"
    patched = apply_frontmatter_patch(original, {"name": "demo", "description": "Demo"})

    assert "trigger: bad" not in patched.split("---", 2)[1]
    assert "# Body\n```json\n{\"trigger\": true}\n```\n" in patched


def test_creator_skill_contract_includes_frontmatter_schema_check():
    content = "---\nname: demo\ndescription: Demo\noutputs: []\n---\n# Demo\n"
    results = validate_file_contract(file_path="SKILL.md", content=content, blueprint_text="")

    failed_ids = {result.id for result in results if not result.passed}
    assert "skill_md.frontmatter.schema" in failed_ids


def test_creator_reference_contract_includes_frontmatter_schema_check():
    content = "---\ntitle: Guide\nrequired_capabilities: [text_generation]\n---\n# Guide\n"
    results = validate_file_contract(file_path="references/guide.md", content=content, blueprint_text="")

    failed_ids = {result.id for result in results if not result.passed}
    assert "reference.frontmatter.schema" in failed_ids
