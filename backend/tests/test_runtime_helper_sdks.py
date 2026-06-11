import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.runtime_tools import (
    analyze_image_with_vision,
    api_get,
    build_pdf_report,
    create_wechat_draft,
    describe_database_table,
    fetch_url_text,
    images_to_pdf,
    list_database_tables,
    ocr_image,
    publish_wechat_draft,
    query_database_readonly,
    read_docx_text,
    read_pptx_text,
    read_spreadsheet,
    upload_wechat_media,
    web_search,
)


def test_new_runtime_helpers_return_mock_data_in_trial_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("SKILL_WORKDIR", str(tmp_path))
    image = tmp_path / "sample.png"
    image.write_bytes(b"not-a-real-image-but-trial-mode-does-not-open-it")

    assert web_search("sample topic", top_k=20)["results"]
    assert fetch_url_text("https://example.com")["text"]
    assert query_database_readonly("SELECT * FROM records")["rows"]
    assert list_database_tables()["tables"]
    assert describe_database_table("records")["columns"]
    assert analyze_image_with_vision(str(image))["description"]
    assert ocr_image(str(image))["ocr_text"]
    assert read_docx_text("input.docx")["text"]
    assert read_pptx_text("input.pptx")["text"]
    assert read_spreadsheet("input.xlsx")["rows"]
    assert images_to_pdf([str(image)])["pdf_path"]
    assert build_pdf_report("Title", [{"title": "A", "text": "B"}])["pdf_path"]
    assert create_wechat_draft("title", "<p>content</p>")["status"] == "draft_created"
    assert publish_wechat_draft("draft") ["status"] == "published"
    assert upload_wechat_media(str(image))["media_id"]

    monkeypatch.setenv("API_FETCH_ALLOWED_HOSTS", "example.com")
    assert api_get("https://example.com/path")["json"] == {"mock": True}
