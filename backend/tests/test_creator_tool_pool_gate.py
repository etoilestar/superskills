from backend.services.creator.tool_pool_gate import gate_tool_request


def test_gate_allows_registered_matching_tool():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'unified_file_text_read'}, file_role='generic_script')
    assert event.decision == 'allow'
    assert event.allowed_helper_imports == ['read_file_text']


def test_gate_allows_custom_tool_import_path_and_function():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'pdf_to_md_mineru'}, file_role='generic_script')
    assert event.decision == 'allow'
    assert event.allowed_import_paths == ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru']
    assert event.allowed_function_imports == ['pdf_to_md_mineru']


def test_gate_rejects_missing_tool():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'read_pdf_text'}, file_role='generic_script')
    assert event.decision == 'not_found'


def test_gate_rejects_reference_runtime_tool():
    event = gate_tool_request({'target_file': 'references/a.md', 'candidate_tool_id': 'unified_file_text_read'}, file_role='reference')
    assert event.decision == 'blocked_by_policy'


def test_gate_role_mismatch_for_custom_tool():
    # gate must NOT reject a valid scripts/** tool just because file_role is an
    # unknown/non-matching component_hint (e.g. 'image_generator').
    # Role is only a logging hint; the allow/deny decision must be driven by
    # tool availability, helper exports, and path policy alone.
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'pdf_to_md_mineru'}, file_role='image_generator')
    assert event.decision == 'allow'
