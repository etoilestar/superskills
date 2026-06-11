import json
from pathlib import Path

from backend.routers.chat_models import ChatRequest, Message
from backend.services.skill_contract import WorkflowContract
from backend.services.skill_creator_dry_run import run_creator_workflow_dry_run


def test_creator_workflow_dry_run(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    (scripts / "make.py").write_text(
        "import json, sys\n"
        "p=json.loads(sys.argv[1])\n"
        "print(json.dumps({'items':[{'title':'A'},{'title':'B'}]}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    (scripts / "use.py").write_text(
        "import json, sys\n"
        "p=json.loads(sys.argv[1])\n"
        "print(json.dumps({'path':'out_'+p['title']+'.txt'}, ensure_ascii=False))\n",
        encoding="utf-8",
    )

    contract = WorkflowContract.from_raw({
        "skill_name": "demo",
        "steps": [
            {
                "id": "make",
                "script_path": "scripts/make.py",
                "inputs": {"topic": {"type": "string"}},
                "outputs": {
                    "items": {
                        "type": "array",
                        "min_items": 1,
                        "items": {"type": "object", "required": ["title"]},
                    }
                },
                "command_template": "python scripts/make.py '{\"topic\":\"{{topic}}\"}'",
            },
            {
                "id": "use",
                "script_path": "scripts/use.py",
                "foreach": {"collection": "make.items", "item_name": "item"},
                "inputs": {"title": {"type": "string", "source": "item.title"}},
                "outputs": {"path": {"type": "string"}},
                "command_template": "python scripts/use.py '{\"title\":\"{{title}}\"}'",
            },
        ],
    })

    result = run_creator_workflow_dry_run(skill_dir=tmp_path, contract=contract, sample_input={"topic": "x"})
    assert result.ok
    assert len(result.traces) == 3


def _write_creator_e2e_fixture(root: Path) -> None:
    (root / "scripts").mkdir()
    (root / "references").mkdir()
    (root / "outputs").mkdir()
    (root / "SKILL.md").write_text(
        "# Creator E2E fixture\n\n"
        "```bash\n"
        "python scripts/parse_and_write.py '{\"payload\":{\"user_request\":\"{{user_request}}\",\"input_files\":\"{{input_files}}\",\"fields\":\"{{fields}}\",\"options\":\"{{options}}\"}}'\n"
        "```\n\n"
        "```bash\n"
        "python scripts/build_file.py '{\"payload\":{\"subject\":\"{{subject}}\",\"draft_text\":\"{{draft_text}}\"}}'\n"
        "```\n",
        encoding="utf-8",
    )
    (root / "references" / "static.md").write_text("static reference\n", encoding="utf-8")
    (root / "scripts" / "parse_and_write.py").write_text(
        "import json, sys\n"
        "payload=json.loads(sys.argv[1])['payload']\n"
        "request=payload['user_request']\n"
        "subject=request.split()[-1] if request.split() else request\n"
        "print(json.dumps({'subject': subject, 'draft_text': f'derived draft for {subject}', 'received_user_request': request}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    (root / "scripts" / "build_file.py").write_text(
        "import json, os, sys\n"
        "payload=json.loads(sys.argv[1])['payload']\n"
        "out_dir=os.environ['OUTPUT_DIR']\n"
        "os.makedirs(out_dir, exist_ok=True)\n"
        "path=os.path.join(out_dir, 'result.pdf')\n"
        "body=(payload['subject']+'\\n'+payload['draft_text']).encode('utf-8')\n"
        "with open(path, 'wb') as f:\n"
        "    f.write(b'%PDF-1.4\\n1 0 obj<</Type/Catalog>>endobj\\n')\n"
        "    f.write(b'% creator dry run\\n')\n"
        "    f.write(body + b'\\n%%EOF\\n')\n"
        "print(json.dumps({'pdf_path':'outputs/result.pdf','file_paths':['outputs/result.pdf'],'file_outputs':['outputs/result.pdf'],'received_subject':payload['subject'],'received_draft_text':payload['draft_text']}, ensure_ascii=False))\n",
        encoding="utf-8",
    )


def test_creator_e2e_uses_sandbox_external_input_contract(tmp_path: Path):
    _write_creator_e2e_fixture(tmp_path)
    request = ChatRequest(messages=[Message(role="user", content="free form request alpha")], input_files=[])
    contract = WorkflowContract.from_raw({
        "skill_name": "creator-e2e-fixture",
        "steps": [
            {
                "id": "parse_and_write",
                "script_path": "scripts/parse_and_write.py",
                "inputs": {"payload": {"type": "object", "required": False, "default": {}}},
                "outputs": {"subject": "string", "draft_text": "string", "received_user_request": "string"},
                "command_template": "python scripts/parse_and_write.py '{\"payload\":{\"user_request\":\"{{user_request}}\",\"input_files\":\"{{input_files}}\",\"fields\":\"{{fields}}\",\"options\":\"{{options}}\"}}'",
            },
            {
                "id": "build_file",
                "script_path": "scripts/build_file.py",
                "inputs": {"payload": {"type": "object", "required": False, "default": {}}},
                "outputs": {
                    "pdf_path": {"type": "file_path", "path_must_exist": True},
                    "file_paths": {"type": "file_paths", "path_must_exist": True},
                    "file_outputs": {"type": "file_paths", "path_must_exist": True},
                    "received_subject": "string",
                    "received_draft_text": "string",
                },
                "command_template": "python scripts/build_file.py '{\"payload\":{\"subject\":\"{{subject}}\",\"draft_text\":\"{{draft_text}}\"}}'",
            },
        ],
    })

    result = run_creator_workflow_dry_run(skill_dir=tmp_path, contract=contract, chat_request=request)

    assert result.ok, result.issues
    assert result.context["user_request"] == "free form request alpha"
    assert result.context["input"] == "free form request alpha"
    assert result.context["text"] == "free form request alpha"
    assert result.context["input_files"] == []
    assert result.context["fields"] == {}
    assert result.traces[0].payload["received_user_request"] == "free form request alpha"
    assert result.traces[0].payload["subject"] == "alpha"
    assert result.traces[1].payload["received_subject"] == "alpha"
    assert result.traces[1].payload["received_draft_text"] == "derived draft for alpha"
    assert (tmp_path / "outputs" / "result.pdf").is_file()
    assert result.traces[1].payload["pdf_path"] == "outputs/result.pdf"
    assert result.traces[1].payload["file_paths"] == ["outputs/result.pdf"]
    assert result.traces[1].payload["file_outputs"] == ["outputs/result.pdf"]
    assert result.output_files == [{"path": "outputs/result.pdf", "url": "/api/skills/creator-e2e-fixture/files/outputs/result.pdf"}]


def test_creator_external_input_does_not_guess_business_fields(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "bad.py").write_text(
        "import json, sys\nprint(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    request = ChatRequest(messages=[Message(role="user", content="free form request alpha")], input_files=[])
    contract = WorkflowContract.from_raw({
        "skill_name": "creator-negative-fixture",
        "steps": [
            {
                "id": "bad",
                "script_path": "scripts/bad.py",
                "inputs": {"required_business_field": {"type": "string"}},
                "outputs": {"ok": "boolean"},
                "command_template": "python scripts/bad.py '{\"required_business_field\":\"{{required_business_field}}\"}'",
            }
        ],
    })

    result = run_creator_workflow_dry_run(skill_dir=tmp_path, contract=contract, chat_request=request)

    assert not result.ok
    assert result.traces == []
    assert result.issues[0]["code"] == "external_input_missing"
    assert "required_business_field" in result.issues[0]["message"]
    assert "不能由模型猜字段" in result.issues[0]["message"]


def test_creator_external_input_accepts_determined_structured_sources(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "echo.py").write_text(
        "import json, sys\np=json.loads(sys.argv[1])\nprint(json.dumps({'text': p['named_value'], 'field_value': p['field_value'], 'option_value': p['option_value']}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    contract = WorkflowContract.from_raw({
        "skill_name": "structured-sources-fixture",
        "steps": [{
            "id": "echo",
            "script_path": "scripts/echo.py",
            "inputs": {},
            "outputs": {"text": "string"},
            "command_template": "python scripts/echo.py '{\"named_value\":\"{{named_value}}\",\"field_value\":\"{{fields.field_value}}\",\"option_value\":\"{{options.option_value}}\"}'",
        }],
    })

    result = run_creator_workflow_dry_run(
        skill_dir=tmp_path,
        contract=contract,
        chat_request={
            "messages": [{"role": "user", "content": '{"named_value":"from-json"}'}],
            "fields": {"field_value": "from-fields", "secret_token": "must-not-leak"},
            "options": {"option_value": "from-options", "api_key": "must-not-leak"},
        },
    )

    assert result.ok, result.issues
    assert result.context["named_value"] == "from-json"
    assert result.context["fields"] == {"named_value": "from-json", "field_value": "from-fields"}
    assert result.context["options"] == {"option_value": "from-options"}
    assert result.traces[0].payload == {"text": "from-json", "field_value": "from-fields", "option_value": "from-options"}


def test_creator_internal_missing_placeholder_reports_e2e_dataflow_missing(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "first.py").write_text(
        "import json\nprint(json.dumps({'actual_internal_name':'ok'}))\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "second.py").write_text(
        "import json\nprint(json.dumps({'text':'ok'}))\n",
        encoding="utf-8",
    )
    contract = WorkflowContract.from_raw({
        "skill_name": "dataflow-negative-fixture",
        "steps": [
            {
                "id": "first",
                "script_path": "scripts/first.py",
                "inputs": {},
                "outputs": {"actual_internal_name": "string"},
                "command_template": "python scripts/first.py '{}'",
            },
            {
                "id": "second",
                "script_path": "scripts/second.py",
                "inputs": {},
                "outputs": {"text": "string"},
                "command_template": "python scripts/second.py '{\"missing\":\"{{missing_internal_name}}\"}'",
            },
        ],
    })

    result = run_creator_workflow_dry_run(skill_dir=tmp_path, contract=contract, chat_request={"messages": []})

    assert not result.ok
    assert result.issues[0]["code"] == "e2e_dataflow_missing"
    assert "missing_internal_name" in result.issues[0]["message"]


def test_creator_artifact_outputs_must_not_be_assets(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "runtime.txt").write_text("runtime artifact in wrong dir", encoding="utf-8")
    (tmp_path / "scripts" / "bad_artifact.py").write_text(
        "import json\nprint(json.dumps({'file_paths':['assets/runtime.txt']}))\n",
        encoding="utf-8",
    )
    contract = WorkflowContract.from_raw({
        "skill_name": "artifact-negative-fixture",
        "steps": [{
            "id": "bad_artifact",
            "script_path": "scripts/bad_artifact.py",
            "inputs": {},
            "outputs": {"file_paths": {"type": "file_paths", "path_must_exist": True}},
            "command_template": "python scripts/bad_artifact.py '{}'",
        }],
    })

    result = run_creator_workflow_dry_run(skill_dir=tmp_path, contract=contract, chat_request={"messages": []})

    assert not result.ok
    assert result.issues[0]["code"] == "file_output_missing"
    assert "OUTPUT_DIR/outputs" in result.issues[0]["message"]


def test_skill_action_and_package_requests_build_same_external_context():
    from backend.routers.creator import (
        PackageSkillRequest,
        SkillActionRequest,
        _external_context_from_skill_action_request,
    )

    payload = {
        "skill_name": "same-envelope-fixture",
        "messages": [{"role": "user", "content": '{"declared":"json"}'}],
        "input_files": [{"path": "inputs/session/data.txt", "filename": "data.txt"}],
        "fields": {"explicit": "field"},
        "options": {"safe_option": "value"},
    }

    validate_context = _external_context_from_skill_action_request(SkillActionRequest(**payload))
    package_context = _external_context_from_skill_action_request(PackageSkillRequest(**payload))

    assert package_context == validate_context
    assert package_context["user_request"] == '{"declared":"json"}'
    assert package_context["files"] == {"data.txt": "inputs/session/data.txt"}
    assert package_context["fields"] == {"declared": "json", "explicit": "field"}
