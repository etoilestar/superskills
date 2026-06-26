"""Document runtime helpers for generated Skill scripts.

The helpers in this module intentionally import optional document libraries
inside each function.  That keeps ``backend.services.skill_runtime`` importable
in lightweight environments while still making Creator tool status aware that
these helpers exist and are platform-owned.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Any, Iterable

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_filename(filename: str, default: str) -> str:
    candidate = _SAFE_FILENAME_RE.sub("-", str(filename or default).strip()).strip("-._")
    return candidate or default


def _output_path(
    *,
    output_path: str | os.PathLike[str] | None,
    output_dir: str | os.PathLike[str] | None,
    filename: str,
) -> Path:
    """Resolve a document output path constrained to OUTPUT_DIR.

    Platform helpers must not write arbitrary host paths before the later
    artifact validator has a chance to reject unsafe stdout declarations.  The
    caller may provide a relative subpath, or an absolute path that is already
    inside OUTPUT_DIR, but traversal/absolute escapes are rejected up front.
    """
    out_dir = Path(output_dir or os.environ.get("OUTPUT_DIR") or "outputs").expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if output_path:
        raw_path = Path(output_path).expanduser()
        candidate = raw_path.resolve() if raw_path.is_absolute() else (out_dir / raw_path).resolve()
        try:
            candidate.relative_to(out_dir)
        except ValueError as exc:
            raise ValueError("output_path must stay under OUTPUT_DIR") from exc
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    return out_dir / _safe_filename(filename, filename)


def _coerce_lines(text: str | Iterable[Any]) -> list[str]:
    if isinstance(text, str):
        lines = text.splitlines()
    else:
        lines = [str(item) for item in text]
    return [line if line else " " for line in lines] or ["Generated document"]



def create_text_file(
    text: str,
    filename: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create a UTF-8 TXT file and return JSON-serializable artifact paths."""
    safe_name = _safe_filename(filename or "output.txt", "output.txt")
    if not safe_name.lower().endswith(".txt"):
        safe_name = f"{safe_name}.txt"
    text_path = _output_path(output_path=None, output_dir=output_dir, filename=safe_name)
    text_path.write_text(str(text or ""), encoding="utf-8")
    return {"text_path": str(text_path), "file_paths": [str(text_path)], "file_outputs": [str(text_path)], "artifact_metadata": {"creator_tool": "create_text_file", "artifact_type": "text", "block_count": 1, "block_types": ["text"], "component_types": ["text"], "styles": {}, "options": {"filename": safe_name}, "referenced_paths": [], "media_items": [], "table_items": [], "heading_levels": [], "layout_options": {}, "constraint_values": {}}}

def _artifact_result(path: Path, *, artifact_type: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        f"{artifact_type}_path": str(path),
        "file_paths": [str(path)],
        "file_outputs": [str(path)],
    }
    if metadata:
        result["artifact_metadata"] = {
            "artifact_type": artifact_type,
            **metadata,
        }
    return result

def _write_minimal_trial_pdf(path: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF\n"
    )
    return _artifact_result(path, artifact_type="pdf", metadata=metadata)


def _normalize_pdf_styles(styles: dict[str, Any] | None) -> dict[str, Any]:
    styles = dict(styles or {})
    return {
        "page_size": str(styles.get("page_size") or "A4"),
        "font_name": str(styles.get("font_name") or styles.get("font_family") or "STSong-Light"),
        "font_path": str(styles.get("font_path") or "").strip(),
        "title_font_size": float(styles.get("title_font_size") or 20),
        "heading_font_size": float(styles.get("heading_font_size") or 16),
        "body_font_size": float(styles.get("body_font_size") or styles.get("font_size") or 12),
        "caption_font_size": float(styles.get("caption_font_size") or 9),
        "line_spacing": float(styles.get("line_spacing") or 1.35),
        "first_line_indent": float(styles.get("first_line_indent") or 0),
        "space_before": float(styles.get("space_before") or 0),
        "space_after": float(styles.get("space_after") or 8),
        "margin_left": float(styles.get("margin_left") or styles.get("margin") or 72),
        "margin_right": float(styles.get("margin_right") or styles.get("margin") or 72),
        "margin_top": float(styles.get("margin_top") or styles.get("margin") or 72),
        "margin_bottom": float(styles.get("margin_bottom") or styles.get("margin") or 72),
        "image_max_width": float(styles.get("image_max_width") or 420),
        "image_max_height": float(styles.get("image_max_height") or 360),
    }


def _normalize_document_blocks(blocks: Any) -> list[dict[str, Any]]:
    if isinstance(blocks, str):
        return [{"type": "paragraph", "text": line} for line in _coerce_lines(blocks)]

    if isinstance(blocks, dict):
        if isinstance(blocks.get("blocks"), list):
            blocks = blocks["blocks"]
        else:
            return [dict(blocks)]

    if not isinstance(blocks, list):
        try:
            blocks = list(blocks)
        except Exception:
            blocks = [{"type": "paragraph", "text": str(blocks or "Generated document")}]

    normalized: list[dict[str, Any]] = []
    for item in blocks:
        if isinstance(item, dict):
            block = dict(item)
        else:
            block = {"type": "paragraph", "text": str(item)}
        block_type = str(block.get("type") or "paragraph").strip().lower()
        block["type"] = block_type
        normalized.append(block)

    return normalized or [{"type": "paragraph", "text": "Generated document"}]


def _register_reportlab_font(font_name: str, font_path: str = "") -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    # Built-in CID fonts work well for Chinese without bundling font files.
    if not font_path:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        except Exception:
            if font_name != "STSong-Light":
                font_name = "STSong-Light"
                pdfmetrics.registerFont(UnicodeCIDFont(font_name))
            else:
                raise
        return font_name

    from reportlab.pdfbase.ttfonts import TTFont

    safe_font_path = _safe_input_path(font_path, {".ttf", ".otf"})
    pdfmetrics.registerFont(TTFont(font_name, str(safe_font_path)))
    return font_name



def _document_requirement_metadata(blocks: list[dict[str, Any]], styles: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    block_types = [str(block.get("type") or "").strip().lower() for block in blocks if isinstance(block, dict)]
    referenced_paths: list[str] = []
    media_items: list[dict[str, Any]] = []
    table_items: list[dict[str, Any]] = []
    heading_levels: list[int] = []
    constraint_values: dict[str, Any] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if block.get("path"):
            referenced_paths.append(str(block.get("path")))
        if btype in {"image", "media", "audio", "video"}:
            media_items.append({k: v for k, v in block.items() if k in {"type", "path", "caption", "width", "height"}})
        if btype == "table":
            rows = block.get("rows") if isinstance(block.get("rows"), list) else []
            table_items.append({"headers": block.get("headers") or [], "row_count": len(rows)})
        if btype == "heading":
            try:
                heading_levels.append(int(block.get("level") or 1))
            except Exception:
                heading_levels.append(1)
        for key, value in block.items():
            if key not in {"text", "content"} and value not in (None, "", [], {}):
                constraint_values.setdefault(key, value)
    return {
        "block_types": block_types,
        "component_types": sorted(set(block_types)),
        "options": dict(options or {}),
        "referenced_paths": referenced_paths,
        "media_items": media_items,
        "table_items": table_items,
        "heading_levels": heading_levels,
        "layout_options": {k: v for k, v in styles.items() if any(token in k for token in ("margin", "spacing", "size", "width", "height", "indent"))},
        "constraint_values": constraint_values,
    }

def create_pdf_document(
    blocks: list[dict[str, Any]] | dict[str, Any] | str | Iterable[Any],
    *,
    styles: dict[str, Any] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pdf",
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a structured PDF document from blocks and return artifact paths.

    Supported block types:
    - title: {"type": "title", "text": "..."}
    - heading: {"type": "heading", "level": 1, "text": "..."}
    - paragraph/text: {"type": "paragraph", "text": "..."}
    - image: {"type": "image", "path": "...", "caption": "..."}
    - table: {"type": "table", "headers": [...], "rows": [[...], ...]}
    - spacer: {"type": "spacer", "height": 12}
    - page_break: {"type": "page_break"}

    Generated Skill scripts should print the returned dict, or include its
    pdf_path/file_outputs fields in stdout JSON.
    """
    pdf_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)
    style_cfg = _normalize_pdf_styles(styles)
    normalized_blocks = _normalize_document_blocks(blocks)

    result_metadata = {
        "creator_tool": "create_pdf_document",
        "block_count": len(normalized_blocks),
        "styles": {
            key: value
            for key, value in style_cfg.items()
            if key != "font_path"
        },
        **_document_requirement_metadata(normalized_blocks, {key: value for key, value in style_cfg.items() if key != "font_path"}, {"filename": filename, "title": title}),
        **(metadata or {}),
    }

    if _trial():
        return _write_minimal_trial_pdf(pdf_path, metadata=result_metadata)

    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        Image as RLImage,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    page_size_name = str(style_cfg["page_size"]).upper()
    page_size = LETTER if page_size_name in {"LETTER", "US_LETTER"} else A4
    font_name = _register_reportlab_font(str(style_cfg["font_name"]), str(style_cfg.get("font_path") or ""))

    base_styles = getSampleStyleSheet()
    body_font_size = float(style_cfg["body_font_size"])
    body_leading = body_font_size * float(style_cfg["line_spacing"])

    title_style = ParagraphStyle(
        "SuperskillsTitle",
        parent=base_styles["Title"],
        fontName=font_name,
        fontSize=float(style_cfg["title_font_size"]),
        leading=float(style_cfg["title_font_size"]) * 1.25,
        spaceAfter=14,
    )
    heading_style = ParagraphStyle(
        "SuperskillsHeading",
        parent=base_styles["Heading1"],
        fontName=font_name,
        fontSize=float(style_cfg["heading_font_size"]),
        leading=float(style_cfg["heading_font_size"]) * 1.25,
        spaceBefore=10,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "SuperskillsBody",
        parent=base_styles["BodyText"],
        fontName=font_name,
        fontSize=body_font_size,
        leading=body_leading,
        firstLineIndent=float(style_cfg["first_line_indent"]),
        spaceBefore=float(style_cfg["space_before"]),
        spaceAfter=float(style_cfg["space_after"]),
    )
    caption_style = ParagraphStyle(
        "SuperskillsCaption",
        parent=base_styles["BodyText"],
        fontName=font_name,
        fontSize=float(style_cfg["caption_font_size"]),
        leading=float(style_cfg["caption_font_size"]) * 1.25,
        spaceBefore=4,
        spaceAfter=8,
    )

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=page_size,
        leftMargin=float(style_cfg["margin_left"]),
        rightMargin=float(style_cfg["margin_right"]),
        topMargin=float(style_cfg["margin_top"]),
        bottomMargin=float(style_cfg["margin_bottom"]),
        title=title or pdf_path.stem,
    )

    story = []

    def paragraph_text(value: Any) -> str:
        return escape(str(value or "")).replace("\n", "<br/>")

    for block in normalized_blocks:
        block_type = str(block.get("type") or "paragraph").lower()

        if block_type == "title":
            story.append(Paragraph(paragraph_text(block.get("text")), title_style))
            continue

        if block_type == "heading":
            story.append(Paragraph(paragraph_text(block.get("text")), heading_style))
            continue

        if block_type in {"paragraph", "text"}:
            text = str(block.get("text") or block.get("content") or "")
            if not text.strip():
                story.append(Spacer(1, float(style_cfg["space_after"])))
            else:
                for part in text.split("\n\n"):
                    story.append(Paragraph(paragraph_text(part), body_style))
            continue

        if block_type == "list":
            items = block.get("items") or []
            if isinstance(items, list):
                flow_items = [
                    ListItem(Paragraph(paragraph_text(item), body_style))
                    for item in items
                ]
                story.append(ListFlowable(flow_items, bulletType="bullet"))
            continue

        if block_type == "image":
            raw_path = str(block.get("path") or block.get("image_path") or "").strip()
            if not raw_path:
                continue
            image_path = _safe_input_path(raw_path, {".png", ".jpg", ".jpeg", ".webp"})
            image = RLImage(str(image_path))
            max_w = float(block.get("max_width") or style_cfg["image_max_width"])
            max_h = float(block.get("max_height") or style_cfg["image_max_height"])
            scale = min(max_w / image.drawWidth, max_h / image.drawHeight, 1.0)
            image.drawWidth *= scale
            image.drawHeight *= scale
            story.append(image)
            if block.get("caption"):
                story.append(Paragraph(paragraph_text(block.get("caption")), caption_style))
            continue

        if block_type == "table":
            headers = block.get("headers") or []
            rows = block.get("rows") or []
            table_data = []
            if headers:
                table_data.append(headers)
            table_data.extend(rows if isinstance(rows, list) else [])
            if table_data:
                rendered_rows = [
                    [Paragraph(paragraph_text(cell), body_style) for cell in row]
                    for row in table_data
                ]
                table = Table(rendered_rows, repeatRows=1 if headers else 0)
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 6),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(table)
                story.append(Spacer(1, 8))
            continue

        if block_type in {"page_break", "pagebreak", "break"}:
            story.append(PageBreak())
            continue

        if block_type == "spacer":
            story.append(Spacer(1, float(block.get("height") or 12)))
            continue

        story.append(Paragraph(paragraph_text(block.get("text") or block), body_style))

    if not story:
        story.append(Paragraph("Generated document", body_style))

    doc.build(story)
    return _artifact_result(pdf_path, artifact_type="pdf", metadata=result_metadata)

def create_pdf(
    text: str | Iterable[Any],
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pdf",
    title: str | None = None,
    font_name: str = "STSong-Light",
    font_size: float = 14,
    line_spacing: float = 1.35,
) -> dict[str, Any]:
    """Create a simple Unicode-capable PDF from plain text.

    This is the compatibility wrapper for simple text-to-PDF tasks.  For
    structured documents with headings, images, tables, or layout requirements,
    generated Skill scripts should use create_pdf_document instead.
    """
    blocks: list[dict[str, Any]] = []
    if title:
        blocks.append({"type": "title", "text": title})
    blocks.extend({"type": "paragraph", "text": line} for line in _coerce_lines(text))

    return create_pdf_document(
        blocks,
        output_path=output_path,
        output_dir=output_dir,
        filename=filename,
        title=title,
        styles={
            "font_name": font_name,
            "body_font_size": font_size,
            "line_spacing": line_spacing,
        },
        metadata={
            "creator_tool": "create_pdf",
            "compatibility_wrapper": True,
        },
    )


def _rows_from_tabular(headers: Any = None, rows: Any = None) -> tuple[list[str], list[list[Any]]]:
    if rows is None:
        rows = []
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    if not isinstance(rows, list):
        rows = list(rows) if rows is not None else []
    if rows and all(isinstance(item, dict) for item in rows):
        columns = [str(value) for value in (headers or [])]
        for row in rows:
            for key in row.keys():
                if str(key) not in columns:
                    columns.append(str(key))
        return columns, [[row.get(column) for column in columns] for row in rows]
    matrix = [list(row) if isinstance(row, (list, tuple)) else [row] for row in rows]
    columns = [str(value) for value in (headers or [])]
    if not columns and matrix:
        columns = [f"Column{index}" for index in range(1, max(len(row) for row in matrix) + 1)]
    return columns, matrix


def create_docx(
    text: str | Iterable[Any] | None = None,
    *,
    blocks: list[dict[str, Any]] | dict[str, Any] | str | Iterable[Any] | None = None,
    paragraphs: Iterable[Any] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.docx",
    title: str | None = None,
) -> dict[str, Any]:
    """Create a Word document with text, blocks, lists, tables, and images."""
    docx_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)
    if not docx_path.name.lower().endswith(".docx"):
        docx_path = docx_path.with_suffix(".docx")

    from docx import Document

    document = Document()
    source_blocks = blocks if blocks is not None else paragraphs if paragraphs is not None else text
    normalized = _normalize_document_blocks(source_blocks or "Generated document")
    if title:
        document.add_heading(str(title), level=1)
    for block in normalized:
        block_type = str(block.get("type") or "paragraph").lower()
        if block_type == "title":
            document.add_heading(str(block.get("text") or ""), level=0)
        elif block_type == "heading":
            level = max(1, min(int(block.get("level") or 1), 9))
            document.add_heading(str(block.get("text") or ""), level=level)
        elif block_type == "list":
            for item in block.get("items") or []:
                document.add_paragraph(str(item), style="List Bullet")
        elif block_type == "table":
            headers, rows = _rows_from_tabular(block.get("headers"), block.get("rows"))
            table_rows = ([headers] if headers else []) + rows
            if table_rows:
                width = max(len(row) for row in table_rows) or 1
                table = document.add_table(rows=len(table_rows), cols=width)
                table.style = "Table Grid"
                for r_idx, row in enumerate(table_rows):
                    for c_idx in range(width):
                        table.cell(r_idx, c_idx).text = str(row[c_idx] if c_idx < len(row) and row[c_idx] is not None else "")
        elif block_type == "image":
            raw_path = str(block.get("path") or block.get("image_path") or "").strip()
            if raw_path:
                document.add_picture(str(_safe_input_path(raw_path, {".png", ".jpg", ".jpeg", ".webp"})))
                if block.get("caption"):
                    document.add_paragraph(str(block.get("caption")))
        else:
            document.add_paragraph(str(block.get("text") or block.get("content") or ""))
    document.save(str(docx_path))
    return _artifact_result(docx_path, artifact_type="docx")


def create_pptx(
    slides: str | Iterable[Any] | None = None,
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    filename: str = "output.pptx",
    title: str = "Generated Presentation",
) -> dict[str, Any]:
    """Create a PowerPoint deck with title, content, list, image, and table slides."""
    pptx_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)
    if not pptx_path.name.lower().endswith(".pptx"):
        pptx_path = pptx_path.with_suffix(".pptx")

    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    normalized = _normalize_document_blocks(slides or [{"type": "title", "text": title}])
    if not any(str(b.get("type")).lower() == "title" for b in normalized):
        normalized.insert(0, {"type": "title", "text": title})
    for index, block in enumerate(normalized, start=1):
        block_type = str(block.get("type") or "content").lower()
        if block_type == "title":
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = str(block.get("text") or title)
            slide.placeholders[1].text = str(block.get("subtitle") or "")
        elif block_type == "image":
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = str(block.get("title") or f"Slide {index}")
            raw_path = str(block.get("path") or block.get("image_path") or "").strip()
            if raw_path:
                slide.shapes.add_picture(str(_safe_input_path(raw_path, {".png", ".jpg", ".jpeg", ".webp"})), Inches(1), Inches(1.5), width=Inches(8))
        elif block_type == "table":
            headers, rows = _rows_from_tabular(block.get("headers"), block.get("rows"))
            data = ([headers] if headers else []) + rows or [[""]]
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = str(block.get("title") or f"Slide {index}")
            table = slide.shapes.add_table(len(data), max(len(r) for r in data), Inches(0.5), Inches(1.5), Inches(9), Inches(4)).table
            for r_idx, row in enumerate(data):
                for c_idx in range(len(table.columns)):
                    table.cell(r_idx, c_idx).text = str(row[c_idx] if c_idx < len(row) and row[c_idx] is not None else "")
        else:
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = str(block.get("title") or block.get("heading") or f"Slide {index}")
            body = slide.placeholders[1].text_frame
            body.clear()
            items = block.get("items") if block_type == "list" else None
            lines = items if isinstance(items, list) else _coerce_lines(block.get("text") or block.get("content") or "")
            for i, line in enumerate(lines):
                para = body.paragraphs[0] if i == 0 else body.add_paragraph()
                para.text = str(line)
                para.level = 0
    prs.save(str(pptx_path))
    return _artifact_result(pptx_path, artifact_type="pptx")


def create_xlsx(sheets: Any = None, *, headers: Any = None, rows: Any = None, output_path: str | os.PathLike[str] | None = None, output_dir: str | os.PathLike[str] | None = None, filename: str = "output.xlsx") -> dict[str, Any]:
    """Create an XLSX workbook from sheets or headers/rows."""
    xlsx_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)
    if not xlsx_path.name.lower().endswith(".xlsx"):
        xlsx_path = xlsx_path.with_suffix(".xlsx")
    from openpyxl import Workbook
    wb = Workbook()
    sheet_specs = sheets if isinstance(sheets, list) and sheets else [{"name": "Sheet1", "headers": headers, "rows": rows or []}]
    wb.remove(wb.active)
    for idx, spec in enumerate(sheet_specs, start=1):
        spec = spec if isinstance(spec, dict) else {"name": f"Sheet{idx}", "rows": spec}
        ws = wb.create_sheet(str(spec.get("name") or f"Sheet{idx}")[:31])
        cols, matrix = _rows_from_tabular(spec.get("headers"), spec.get("rows"))
        if cols:
            ws.append(cols)
        for row in matrix:
            ws.append(list(row))
    wb.save(str(xlsx_path))
    return _artifact_result(xlsx_path, artifact_type="xlsx")


def create_csv(headers: Any = None, rows: Any = None, *, output_path: str | os.PathLike[str] | None = None, output_dir: str | os.PathLike[str] | None = None, filename: str = "output.csv", encoding: str = "utf-8") -> dict[str, Any]:
    """Create a CSV file from headers and dict/list rows using the stdlib csv module."""
    csv_path = _output_path(output_path=output_path, output_dir=output_dir, filename=filename)
    if not csv_path.name.lower().endswith(".csv"):
        csv_path = csv_path.with_suffix(".csv")
    columns, matrix = _rows_from_tabular(headers, rows or [])
    with csv_path.open("w", newline="", encoding=encoding) as file_obj:
        writer = csv.writer(file_obj)
        if columns:
            writer.writerow(columns)
        writer.writerows(matrix)
    return _artifact_result(csv_path, artifact_type="csv")


def extract_pdf_text(path: str | os.PathLike[str], *, max_pages: int | None = None) -> dict[str, Any]:
    """Extract text from a PDF with pypdf and return page-level text."""
    from pypdf import PdfReader

    pdf_path = Path(path).expanduser().resolve()
    reader = PdfReader(str(pdf_path))
    pages = []
    for index, page in enumerate(reader.pages):
        if max_pages is not None and index >= max_pages:
            break
        pages.append(page.extract_text() or "")
    return {
        "text": "\n".join(pages).strip(),
        "pages": pages,
        "page_count": len(reader.pages),
        "pdf_path": str(pdf_path),
    }

_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_INPUT_FILES = 50


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _allowed_input_roots() -> list[Path]:
    roots = [Path.cwd()]
    for name in ("SKILL_WORKDIR", "SKILL_DIR", "INPUT_DIR", "UPLOAD_DIR", "OUTPUT_DIR"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]))
    roots.extend([Path.cwd() / "inputs", Path.cwd() / "assets", Path.cwd() / "uploads"])
    return [root.expanduser().resolve() for root in roots]


def _safe_input_path(path: str | os.PathLike[str], suffixes: set[str]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.suffix.lower() not in suffixes:
        raise ValueError(f"unsupported file type: {resolved.suffix}")
    if not resolved.is_file():
        raise FileNotFoundError("input file does not exist")
    if resolved.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("input file is too large")
    if not any(resolved == root or resolved.is_relative_to(root) for root in _allowed_input_roots()):
        raise ValueError("input path must stay under the skill workdir, inputs, assets, uploads, or OUTPUT_DIR")
    return resolved


def read_docx_text(docx_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read text from a Word document."""
    if _trial():
        return {"text": "Mock DOCX text during SKILL_TRIAL_RUN.", "paragraphs": ["Mock DOCX text during SKILL_TRIAL_RUN."], "source_path": str(docx_path)}
    path = _safe_input_path(docx_path, {".docx"})
    from docx import Document

    document = Document(str(path))
    paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    return {"text": "\n".join(paragraphs), "paragraphs": paragraphs, "source_path": str(path)}


def read_pptx_text(pptx_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read text from a PowerPoint deck."""
    if _trial():
        return {"text": "Mock PPTX text during SKILL_TRIAL_RUN.", "paragraphs": ["Mock PPTX text during SKILL_TRIAL_RUN."], "source_path": str(pptx_path)}
    path = _safe_input_path(pptx_path, {".pptx"})
    from pptx import Presentation

    paragraphs: list[str] = []
    for slide in Presentation(str(path)).slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                paragraphs.append(str(shape.text))
    return {"text": "\n".join(paragraphs), "paragraphs": paragraphs, "source_path": str(path)}


def read_spreadsheet(path: str | os.PathLike[str], sheet_name: str | None = None, max_rows: int = 500) -> dict[str, Any]:
    """Read rows from an Excel spreadsheet."""
    max_rows = max(1, min(int(max_rows or 500), 5000))
    if _trial():
        return {"sheets": [sheet_name or "Sheet1"], "columns": ["A", "B"], "rows": [{"A": "mock", "B": "value"}], "row_count": 1, "truncated": False}
    safe_path = _safe_input_path(path, {".xlsx", ".xlsm"})
    from openpyxl import load_workbook

    workbook = load_workbook(str(safe_path), read_only=True, data_only=True)
    worksheet = workbook[sheet_name] if sheet_name else workbook[workbook.sheetnames[0]]
    rows_iter = worksheet.iter_rows(values_only=True)
    header_values = next(rows_iter, None) or []
    columns = [str(value) if value not in (None, "") else f"Column{index}" for index, value in enumerate(header_values, start=1)]
    rows: list[dict[str, Any]] = []
    truncated = False
    for index, values in enumerate(rows_iter, start=1):
        if index > max_rows:
            truncated = True
            break
        rows.append({columns[i] if i < len(columns) else f"Column{i+1}": value for i, value in enumerate(values)})
    return {"sheets": workbook.sheetnames, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated, "source_path": str(safe_path)}


def read_csv(path: str | os.PathLike[str], max_rows: int = 500, encoding: str = "utf-8") -> dict[str, Any]:
    """Read a CSV file into structured rows and columns."""
    max_rows = max(1, min(int(max_rows or 500), 5000))
    if _trial():
        return {"columns": ["A", "B"], "rows": [{"A": "mock", "B": "value"}], "row_count": 1, "truncated": False, "text": "A,B\nmock,value", "source_path": str(path)}
    safe_path = _safe_input_path(path, {".csv", ".tsv"})
    delimiter = "\t" if safe_path.suffix.lower() == ".tsv" else ","
    with safe_path.open(newline="", encoding=encoding) as file_obj:
        reader = csv.reader(file_obj, delimiter=delimiter)
        header = next(reader, [])
        columns = [str(value) if value not in (None, "") else f"Column{index}" for index, value in enumerate(header, start=1)]
        rows = []
        truncated = False
        for index, values in enumerate(reader, start=1):
            if index > max_rows:
                truncated = True
                break
            rows.append({columns[i] if i < len(columns) else f"Column{i+1}": value for i, value in enumerate(values)})
    text = "\n".join([",".join(columns)] + [",".join(str(row.get(column, "")) for column in columns) for row in rows])
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated, "text": text, "source_path": str(safe_path)}


def merge_pdfs(pdf_paths: list[str], output_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Merge PDFs into a PDF under OUTPUT_DIR."""
    if not pdf_paths or len(pdf_paths) > _MAX_INPUT_FILES:
        raise ValueError("pdf_paths must contain 1 to 50 files")
    out_path = _output_path(output_path=output_path, output_dir=None, filename="merged.pdf")
    if _trial():
        return _write_minimal_trial_pdf(out_path)
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for raw_path in pdf_paths:
        reader = PdfReader(str(_safe_input_path(raw_path, {".pdf"})))
        for page in reader.pages:
            writer.add_page(page)
    with out_path.open("wb") as file_obj:
        writer.write(file_obj)
    return {"pdf_path": str(out_path), "file_paths": [str(out_path)], "file_outputs": [str(out_path)]}


def images_to_pdf(image_paths: list[str], output_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Convert images to one PDF under OUTPUT_DIR."""
    if not image_paths or len(image_paths) > _MAX_INPUT_FILES:
        raise ValueError("image_paths must contain 1 to 50 files")
    out_path = _output_path(output_path=output_path, output_dir=None, filename="images.pdf")
    if _trial():
        return _write_minimal_trial_pdf(out_path)
    from PIL import Image

    images = []
    for raw_path in image_paths:
        image = Image.open(_safe_input_path(raw_path, {".png", ".jpg", ".jpeg", ".webp"})).convert("RGB")
        images.append(image)
    first, rest = images[0], images[1:]
    first.save(str(out_path), save_all=True, append_images=rest)
    return {"pdf_path": str(out_path), "file_paths": [str(out_path)], "file_outputs": [str(out_path)]}


def build_pdf_report(
    title: str,
    sections: list[dict],
    image_paths: list[str] | None = None,
    *,
    filename: str = "report.pdf",
    styles: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured PDF report from sections and optional images."""
    blocks: list[dict[str, Any]] = [{"type": "title", "text": str(title or "Report")}]

    for section in sections or []:
        section_title = str(section.get("title") or "Section")
        section_text = str(section.get("text") or section.get("content") or "")
        blocks.append({"type": "heading", "text": section_title})
        blocks.append({"type": "paragraph", "text": section_text})

        section_images = section.get("image_paths") or section.get("images") or []
        if isinstance(section_images, str):
            section_images = [section_images]
        if isinstance(section_images, list):
            for image_path in section_images[:_MAX_INPUT_FILES]:
                blocks.append(
                    {
                        "type": "image",
                        "path": str(image_path),
                        "caption": section.get("image_caption") or "",
                    }
                )

    for image_path in (image_paths or [])[:_MAX_INPUT_FILES]:
        blocks.append({"type": "image", "path": str(image_path)})

    return create_pdf_document(
        blocks,
        styles=styles,
        filename=filename or "report.pdf",
        title=title or "Report",
        metadata={
            "creator_tool": "build_pdf_report",
            "section_count": len(sections or []),
            "image_count": len(image_paths or []),
        },
    )
