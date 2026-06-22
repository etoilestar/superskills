from backend.services.markdown_metadata import (
    apply_frontmatter_patch,
    parse_frontmatter,
    validate_reference_frontmatter,
    validate_skill_frontmatter,
)


def test_skill_frontmatter_only_name_description_passes():
    meta, body, had = parse_frontmatter("---\nname: demo\ndescription: ok\n---\n# Body\n")
    assert had
    assert body == "# Body\n"
    assert validate_skill_frontmatter(meta) == []


def test_skill_frontmatter_creator_planning_keys_fail():
    meta, _body, _had = parse_frontmatter("---\nname: demo\ndescription: ok\ntrigger: now\ninputs: []\n---\n# Body\n")
    errors = validate_skill_frontmatter(meta)
    assert any("trigger" in err for err in errors)
    assert any("inputs" in err for err in errors)


def test_reference_without_frontmatter_passes():
    meta, body, had = parse_frontmatter("# Reference\ncontent")
    assert not had
    assert body == "# Reference\ncontent"
    assert validate_reference_frontmatter(meta) == []


def test_reference_document_metadata_passes():
    meta, _body, _had = parse_frontmatter("---\ntitle: Ref\ndescription: helpful\n---\n# Body\n")
    assert validate_reference_frontmatter(meta) == []


def test_reference_creator_planning_key_fails():
    meta, _body, _had = parse_frontmatter("---\ntitle: Ref\nrequired_capabilities: [x]\n---\n# Body\n")
    errors = validate_reference_frontmatter(meta)
    assert any("required_capabilities" in err for err in errors)


def test_apply_frontmatter_patch_preserves_body_and_code_blocks():
    original = "---\nname: old\ndescription: old\ntrigger: bad\n---\n# Body\n```yaml\ntrigger: keep in code\n```\n"
    patched = apply_frontmatter_patch(original, {"name": "new", "description": "new"})
    assert "trigger: bad" not in patched.split("---", 2)[1]
    assert "# Body\n```yaml\ntrigger: keep in code\n```\n" in patched
