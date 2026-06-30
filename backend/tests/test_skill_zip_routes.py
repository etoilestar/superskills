import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _create_skill(skills_path: Path, name: str = "route-skill") -> Path:
    skill_dir = skills_path / name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: route\n---\n", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n", encoding="utf-8"
    )
    return skill_dir


def _zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _client_with_paths(tmp_path: Path):
    from backend.config import settings
    from backend.main import app

    skills_path = tmp_path / "skills"
    exports_path = tmp_path / "exports"
    skills_path.mkdir()
    exports_path.mkdir()
    patches = [
        patch.object(settings, "skills_path", skills_path),
        patch.object(settings, "managed_skills_path", skills_path),
        patch.object(settings, "workspace_skills_path", tmp_path / "workspace"),
        patch.object(settings, "shared_skills_path", tmp_path / "shared"),
        patch.object(settings, "bundled_skills_path", tmp_path / "bundled"),
        patch.object(settings, "governance_path", tmp_path / "governance"),
        patch.object(settings, "exports_path", exports_path),
    ]
    for p in patches:
        p.start()
    for path in [settings.workspace_skills_path, settings.shared_skills_path, settings.bundled_skills_path, settings.governance_path]:
        path.mkdir(parents=True, exist_ok=True)
    return TestClient(app), skills_path, exports_path, patches


def test_export_endpoint_returns_portable_zip(tmp_path):
    client, skills_path, _exports_path, patches = _client_with_paths(tmp_path)
    try:
        _create_skill(skills_path)
        response = client.get("/api/skills/route-skill/export?portable=true")
    finally:
        for p in reversed(patches):
            p.stop()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert 'filename="route-skill.portable.inline.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
    assert "route-skill/SKILL.md" in names
    assert "route-skill/skill-portability.json" in names
    assert not any(name.startswith("route-skill/_portable_runtime/") for name in names)


def test_save_zip_endpoint_writes_outside_skill_dir_and_saved_download_works(tmp_path):
    client, skills_path, exports_path, patches = _client_with_paths(tmp_path)
    try:
        skill_dir = _create_skill(skills_path)
        response = client.post("/api/skills/route-skill/save-zip?portable=true")
        payload = response.json()
        download = client.get(payload["download_url"])
        traversal = client.get("/api/skills/route-skill/saved-zips/..%2Fevil.zip")
        non_zip = client.get("/api/skills/route-skill/saved-zips/not-a-zip.txt")
    finally:
        for p in reversed(patches):
            p.stop()
    assert response.status_code == 200
    assert payload["success"] is True
    saved = Path(payload["path"])
    assert saved.is_file()
    assert saved.suffix == ".zip"
    assert skill_dir not in saved.parents
    assert exports_path in saved.parents
    assert payload["download_url"].endswith(f"/saved-zips/{payload['filename']}")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")
    assert traversal.status_code in {400, 404}
    assert non_zip.status_code == 400


def test_upgrade_endpoint_still_requires_upload_and_rejects_name_mismatch(tmp_path):
    client, skills_path, _exports_path, patches = _client_with_paths(tmp_path)
    try:
        _create_skill(skills_path, "route-skill")
        missing_upload = client.post("/api/skills/route-skill/upgrade")
        mismatch_zip = _zip_bytes({"SKILL.md": "---\nname: other-skill\ndescription: x\n---\n"})
        mismatch = client.post(
            "/api/skills/route-skill/upgrade",
            files={"file": ("other.zip", mismatch_zip, "application/zip")},
        )
    finally:
        for p in reversed(patches):
            p.stop()
    assert missing_upload.status_code == 422
    assert mismatch.status_code == 400
    assert "与目标 'route-skill' 不一致" in mismatch.json()["detail"]
