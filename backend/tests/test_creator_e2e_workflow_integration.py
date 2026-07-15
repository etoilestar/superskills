import json
import sys
from pathlib import Path

from backend.services.creator import e2e


def _write_skill(skill_dir: Path) -> None:
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (skill_dir / "outputs").mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: trace-skill
description: Trace skill
---

Run step 1:
```bash
python scripts/step1.py '{"topic":"{{user_request}}"}'
```

Run step 2:
```bash
python scripts/step2.py '{"content":"{{story_text}}"}'
```
""",
        encoding="utf-8",
    )
    (scripts / "step1.py").write_text(
        """
import json
import sys
from pathlib import Path
from backend.services.runtime_tools import strict_json_argv_guard

SPEC = {"topic": {"type": "string", "required": True}}

def run(args):
    root = Path(__file__).resolve().parents[1]
    counter = root / "outputs" / "step1_count.txt"
    count = int(counter.read_text() or "0") if counter.exists() else 0
    counter.write_text(str(count + 1))
    return {"story_text": f"story about {args['topic']}"}

def main():
    payload = json.loads(sys.argv[1])
    args = strict_json_argv_guard(payload, SPEC)
    print(json.dumps(run(args)))

if __name__ == "__main__":
    main()
""".strip(),
        encoding="utf-8",
    )
    (scripts / "step2.py").write_text(
        """
import json
import sys
from pathlib import Path
from backend.services.runtime_tools import strict_json_argv_guard

SPEC = {"content": {"type": "string", "required": True}}

def run(args):
    root = Path(__file__).resolve().parents[1]
    out = root / "outputs" / "result.dat"
    out.write_text(args["content"], encoding="utf-8")
    return {"file_outputs": ["outputs/result.dat"]}

def main():
    payload = json.loads(sys.argv[1])
    args = strict_json_argv_guard(payload, SPEC)
    print(json.dumps(run(args)))

if __name__ == "__main__":
    main()
""".strip(),
        encoding="utf-8",
    )


def test_two_step_e2e_trace_files_verified_bindings_and_checkpoint_reuse(tmp_path, monkeypatch):
    source = tmp_path / "trace-skill"
    source.mkdir()
    _write_skill(source)

    monkeypatch.setattr(e2e, "_get_skill_venv_python", lambda _skill_dir: Path(sys.executable))
    monkeypatch.setattr(e2e, "_install_capability_dependencies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(e2e, "_install_declared_dependency_packages", lambda *_args, **_kwargs: None)

    session = e2e._create_e2e_session("trace-skill", source_skill_dir=source)
    try:
        errors = e2e._run_skill_workflow_e2e_once(
            "trace-skill",
            source_skill_dir=source,
            external_context={"user_request": "moons"},
            e2e_session=session,
        )
        assert errors == []

        step2_trace_events = [event for event in session.events if event.get("event") == "checkpoint_saved" and event.get("step_index") == 2]
        assert step2_trace_events
        assert session.verified_bindings_by_script["scripts/step2.py"] == {"content": "story_text"}

        step2_checkpoint = json.loads(e2e._checkpoint_path(session, 2).read_text(encoding="utf-8"))
        assert step2_checkpoint["status"] == "passed"
        assert step2_checkpoint["rendered_argv"] == {"content": "story about moons"}
        assert step2_checkpoint["verified_bindings"] == {"content": "story_text"}
        assert step2_checkpoint["runtime_binding_trace"]["content"]["source_root"] == "story_text"
        assert step2_checkpoint["runtime_binding_trace"]["content"]["source_provenance"]["producer_script"] == "scripts/step1.py"
        assert step2_checkpoint["runtime_binding_trace"]["content"]["source_provenance"]["source_kind"] == "stdout"
        assert "story_text" in step2_checkpoint["value_provenance"]
        assert any(item["relative_path"] == "outputs/result.dat" for item in step2_checkpoint["filesystem_diff"]["created_files"])

        # Validate the path resolver against the real created artifact from the run.
        resolved = e2e.resolve_reported_artifact_paths(["outputs/result.dat"], root=session.workspace_dir)
        assert resolved[0]["exists"] is True

        step1_counter = session.workspace_dir / "outputs" / "step1_count.txt"
        assert step1_counter.read_text() == "1"
        session.verified_bindings_by_script.clear()

        errors = e2e._run_skill_workflow_e2e_once(
            "trace-skill",
            source_skill_dir=source,
            external_context={"user_request": "moons"},
            e2e_session=session,
            resume_from_step=2,
        )
        assert errors == []
        assert step1_counter.read_text() == "1"
        assert session.verified_bindings_by_script["scripts/step2.py"] == {"content": "story_text"}
        step1_checkpoint = json.loads(e2e._checkpoint_path(session, 1).read_text(encoding="utf-8"))
        assert step1_checkpoint["status"] == "passed"
        assert step1_checkpoint["runtime_binding_trace"]["topic"]["source_provenance"]["source_kind"] == "external_context"
        assert "story_text" in step1_checkpoint["context_after"]
        assert "story_text" in step1_checkpoint["value_provenance"]
    finally:
        if session.temp_handle is not None:
            session.temp_handle.cleanup()
