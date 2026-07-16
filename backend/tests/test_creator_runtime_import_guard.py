from backend.services.creator.runtime_import_guard import guard_runtime_imports
from backend.services.creator_tool_registry import (
    ToolCapability,
    ToolFunctionManifest,
    clear_registered_tool_capabilities,
    register_tool_capability,
)


def _register_guard_tool(name, function_name, import_path, signature=None, input_schema=None):
    register_tool_capability(ToolCapability(
        name=name,
        display_name=name,
        category="test",
        functions=[ToolFunctionManifest(
            function_name=function_name,
            import_path=import_path,
            short_description="test",
            when_to_use="test",
            signature=signature or f"{function_name}() -> dict",
            input_schema=input_schema or {},
            output_schema={"type": "object"},
            required_capabilities=[name],
        )],
    ))


def test_import_guard_blocks_invented_helpers():
    result = guard_runtime_imports('from backend.services.runtime_tools import read_pdf_text\n', 'scripts/a.py', {'available_tools': [{'tool_id': 'pdf_reader', 'function_name': 'extract_pdf_text', 'import_path': 'backend.services.runtime_tools', 'input_schema': {}, 'output_schema': {}}]})
    assert not result.success
    assert result.error_type == 'generated_unknown_runtime_tool_import'
    assert 'read_pdf_text' in result.missing_imports
    assert result.suggested_replacements == []


def test_import_guard_blocks_star_and_pool_forbidden():
    result = guard_runtime_imports('from backend.services.runtime_tools import *\n', 'scripts/a.py', {'available_tools': [{'tool_id': 'pdf_reader', 'function_name': 'extract_pdf_text', 'import_path': 'backend.services.runtime_tools', 'input_schema': {}, 'output_schema': {}}]})
    assert not result.success
    assert '*' in result.forbidden_imports


def test_import_guard_allows_bound_helper():
    clear_registered_tool_capabilities()
    _register_guard_tool("file_reader", "read_file_text", "backend.services.runtime_tools")
    result = guard_runtime_imports('from backend.services.runtime_tools import read_file_text\n', 'scripts/a.py', {'available_tools': [{'tool_id': 'file_reader', 'function_name': 'read_file_text', 'import_path': 'backend.services.runtime_tools', 'input_schema': {}, 'output_schema': {}}]})
    assert result.success


def test_import_guard_blocks_mixed_bound_and_unbound_runtime_helpers():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text, read_pdf_text\n',
        'scripts/a.py',
        {'available_tools': [{'tool_id': 'file_reader', 'function_name': 'read_file_text', 'import_path': 'backend.services.runtime_tools', 'input_schema': {}, 'output_schema': {}}]},
    )
    assert not result.success
    assert result.error_type == 'generated_unknown_runtime_tool_import'
    assert 'read_pdf_text' in result.missing_imports


def test_import_guard_allows_bound_custom_tool():
    src = 'from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import pdf_to_md_mineru\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'available_tools': [{'tool_id': 'pdf_to_md_mineru', 'function_name': 'pdf_to_md_mineru', 'import_path': 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru', 'input_schema': {}, 'output_schema': {}}]})
    assert result.success


def test_import_guard_allows_custom_tool_full_function_path_and_diagnoses_same_module_unbound_function():
    clear_registered_tool_capabilities()
    _register_guard_tool("lookup", "lookup_value", "backend.services.runtime_tools.custom_tools.lookup")
    ok = guard_runtime_imports(
        'from backend.services.runtime_tools.custom_tools.lookup import lookup_value\n',
        'scripts/a.py',
        {'available_tools': [{'tool_id': 'lookup', 'function_name': 'lookup_value', 'import_path': 'backend.services.runtime_tools.custom_tools.lookup', 'input_schema': {}, 'output_schema': {}}]},
    )
    assert ok.success

    bad = guard_runtime_imports(
        'from backend.services.runtime_tools.custom_tools.lookup import lookup_value, other_value\n',
        'scripts/a.py',
        {'available_tools': [{'tool_id': 'lookup', 'function_name': 'lookup_value', 'import_path': 'backend.services.runtime_tools.custom_tools.lookup', 'input_schema': {}, 'output_schema': {}}]},
    )
    assert not bad.success
    assert bad.error_type == 'generated_tool_import_not_in_available_tools'
    assert 'backend.services.runtime_tools.custom_tools.lookup.other_value' in bad.forbidden_imports


def test_import_guard_diagnoses_unbound_custom_tool_but_blocks_wildcard():
    src = 'from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import pdf_to_md_mineru\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'available_tools': []})
    assert not result.success
    assert result.error_type == 'generated_tool_import_not_in_available_tools'
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru.pdf_to_md_mineru' in result.forbidden_imports
    wildcard = guard_runtime_imports('from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import *\n', 'scripts/a.py', {'available_tools': [{'tool_id': 'pdf_to_md_mineru', 'function_name': 'pdf_to_md_mineru', 'import_path': 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru', 'input_schema': {}, 'output_schema': {}}]})
    assert not wildcard.success
    assert wildcard.error_type == 'generated_custom_tool_wildcard_import'


def test_import_guard_reports_pool_external_custom_tool_as_diagnostic():
    src = 'from backend.services.runtime_tools.custom_tools.https_google_serper_dev_search import https_google_serper_dev_search\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'available_tools': [{'tool_id': 'pdf_to_md_mineru', 'function_name': 'pdf_to_md_mineru', 'import_path': 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru', 'input_schema': {}, 'output_schema': {}}]})
    assert not result.success
    assert result.error_type == 'generated_tool_import_not_in_available_tools'
    assert 'backend.services.runtime_tools.custom_tools.https_google_serper_dev_search.https_google_serper_dev_search' in result.forbidden_imports


def test_import_guard_rejects_unbound_read_file_text():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text\n',
        'scripts/a.py',
        {'available_tools': []},
    )
    assert not result.success
    assert result.error_type == 'generated_tool_import_not_in_available_tools'
    assert 'backend.services.runtime_tools.read_file_text' in result.forbidden_imports


def test_import_guard_allows_open_for_txt_without_bound_helper():
    src = """
def read_txt(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()
"""
    result = guard_runtime_imports(src, 'scripts/a.py', {'available_tools': []})
    assert result.success


def test_import_guard_allows_docx_stdlib_fallback_without_bound_helper():
    src = """
import zipfile
import xml.etree.ElementTree as ET

def read_docx(path):
    with zipfile.ZipFile(path) as zf:
        xml = zf.read('word/document.xml')
    root = ET.fromstring(xml)
    return ''.join(node.text or '' for node in root.iter())
"""
    result = guard_runtime_imports(src, 'scripts/a.py', {'available_tools': []})
    assert result.success


def test_unbound_runtime_helper_is_rejected_by_available_tools():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text\n',
        'scripts/a.py',
        {'available_tools': []},
    )
    assert not result.success
    assert result.error_type == 'generated_tool_import_not_in_available_tools'
    assert 'backend.services.runtime_tools.read_file_text' in result.forbidden_imports


def test_import_guard_allows_any_bound_import_path_and_function_name():
    clear_registered_tool_capabilities()
    _register_guard_tool("arbitrary", "arbitrary_callable", "backend.services.skill_runtime")
    src = 'from backend.services.skill_runtime import arbitrary_callable\n'
    result = guard_runtime_imports(
        src,
        'scripts/a.py',
        {'available_tools': [{'tool_id': 'arbitrary', 'function_name': 'arbitrary_callable', 'import_path': 'backend.services.skill_runtime', 'input_schema': {}, 'output_schema': {}}]},
    )
    assert result.success


def test_import_guard_has_no_business_helper_whitelist_or_replacement_table():
    import backend.services.creator.runtime_import_guard as module

    assert not hasattr(module, '_ALLOWED_SKILL_RUNTIME_HELPERS')
    assert not hasattr(module, 'SUGGESTED_REPLACEMENTS')


def test_arbitrary_platform_import_outside_available_tools_is_rejected():
    src = 'from backend.services.skill_runtime import arbitrary_callable\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'available_tools': []})
    assert not result.success
    assert result.error_type == 'generated_tool_import_not_in_available_tools'


def test_import_guard_rejects_bound_tool_call_missing_required_schema_key():
    clear_registered_tool_capabilities()
    _register_guard_tool(
        "lookup",
        "lookup_value",
        "backend.services.runtime_tools.custom_tools.lookup",
        signature="lookup_value(query: str) -> dict",
        input_schema={'type': 'object', 'required': ['query'], 'properties': {'query': {'type': 'string'}}},
    )
    src = """from backend.services.runtime_tools.custom_tools.lookup import lookup_value

lookup_value()
"""
    result = guard_runtime_imports(
        src,
        'scripts/a.py',
        {'available_tools': [{
            'tool_id': 'lookup.lookup_value',
            'function_name': 'lookup_value',
            'import_path': 'backend.services.runtime_tools.custom_tools.lookup',
            'input_schema': {'type': 'object', 'required': ['query'], 'properties': {'query': {'type': 'string'}}},
            'output_schema': {'type': 'object'},
        }]},
    )
    assert not result.success
    assert result.error_type == 'generated_tool_call_schema_mismatch'
    assert 'query' in result.forbidden_imports[0]


def test_import_guard_accepts_positional_and_keyword_tool_arguments():
    clear_registered_tool_capabilities()
    _register_guard_tool(
        "lookup",
        "lookup_value",
        "backend.services.runtime_tools.custom_tools.lookup",
        signature="lookup_value(query: str, limit: int = 10) -> dict",
        input_schema={'type': 'object', 'required': ['query'], 'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}}},
    )
    binding = {'available_tools': [{'tool_id': 'lookup.lookup_value', 'function_name': 'lookup_value'}]}

    positional = guard_runtime_imports(
        "from backend.services.runtime_tools.custom_tools.lookup import lookup_value\nlookup_value('abc')\n",
        'scripts/a.py',
        binding,
    )
    keyword = guard_runtime_imports(
        "from backend.services.runtime_tools.custom_tools.lookup import lookup_value\nlookup_value(query='abc')\n",
        'scripts/a.py',
        binding,
    )

    assert positional.success
    assert keyword.success


def test_import_guard_rejects_unexpected_and_duplicate_tool_arguments():
    clear_registered_tool_capabilities()
    _register_guard_tool(
        "lookup",
        "lookup_value",
        "backend.services.runtime_tools.custom_tools.lookup",
        signature="lookup_value(query: str, limit: int = 10) -> dict",
        input_schema={'type': 'object', 'required': ['query'], 'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}}},
    )
    binding = {'available_tools': [{'tool_id': 'lookup.lookup_value', 'function_name': 'lookup_value'}]}

    unexpected = guard_runtime_imports(
        "from backend.services.runtime_tools.custom_tools.lookup import lookup_value\nlookup_value(query='abc', unknown=True)\n",
        'scripts/a.py',
        binding,
    )
    duplicate = guard_runtime_imports(
        "from backend.services.runtime_tools.custom_tools.lookup import lookup_value\nlookup_value('abc', query='def')\n",
        'scripts/a.py',
        binding,
    )

    assert not unexpected.success
    assert unexpected.error_type == 'generated_tool_call_schema_mismatch'
    assert 'unexpected arguments' in unexpected.forbidden_imports[0]
    assert not duplicate.success
    assert duplicate.error_type == 'generated_tool_call_schema_mismatch'
    assert 'duplicate arguments' in duplicate.forbidden_imports[0]
