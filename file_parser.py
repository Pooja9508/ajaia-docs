"""Convert uploaded files (.txt / .md / .docx) into Quill Delta JSON strings."""

import json
import re


def txt_to_delta(content: str) -> str:
    ops = []
    for line in content.splitlines():
        if line:
            ops.append({"insert": line})
        ops.append({"insert": "\n"})
    if not ops:
        ops = [{"insert": "\n"}]
    return json.dumps({"ops": ops})


def md_to_delta(content: str) -> str:
    ops = []
    for line in content.splitlines():
        h1 = re.match(r"^# (.+)", line)
        h2 = re.match(r"^## (.+)", line)
        h3 = re.match(r"^### (.+)", line)
        bullet = re.match(r"^[-*] (.+)", line)
        numbered = re.match(r"^\d+\. (.+)", line)

        if h1:
            ops += _inline_ops(h1.group(1))
            ops.append({"insert": "\n", "attributes": {"header": 1}})
        elif h2:
            ops += _inline_ops(h2.group(1))
            ops.append({"insert": "\n", "attributes": {"header": 2}})
        elif h3:
            ops += _inline_ops(h3.group(1))
            ops.append({"insert": "\n", "attributes": {"header": 3}})
        elif bullet:
            ops += _inline_ops(bullet.group(1))
            ops.append({"insert": "\n", "attributes": {"list": "bullet"}})
        elif numbered:
            ops += _inline_ops(numbered.group(1))
            ops.append({"insert": "\n", "attributes": {"list": "ordered"}})
        else:
            ops += _inline_ops(line)
            ops.append({"insert": "\n"})

    if not ops:
        ops = [{"insert": "\n"}]
    return json.dumps({"ops": ops})


def _inline_ops(text: str) -> list:
    """Parse bold (**text**) and italic (*text*) inline markers into Delta ops."""
    ops = []
    # Combined pattern: bold-italic, bold, italic
    pattern = re.compile(r"\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*")
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            ops.append({"insert": text[last : m.start()]})
        if m.group(1):
            ops.append({"insert": m.group(1), "attributes": {"bold": True, "italic": True}})
        elif m.group(2):
            ops.append({"insert": m.group(2), "attributes": {"bold": True}})
        elif m.group(3):
            ops.append({"insert": m.group(3), "attributes": {"italic": True}})
        last = m.end()
    if last < len(text):
        ops.append({"insert": text[last:]})
    if not ops:
        ops.append({"insert": text or ""})
    return ops


def docx_to_delta(file_path: str) -> str:
    try:
        from docx import Document
        from docx.shared import RGBColor
    except ImportError:
        raise ValueError("python-docx is required to parse .docx files")

    doc = Document(file_path)
    ops = []

    for para in doc.paragraphs:
        style = para.style.name.lower() if para.style else ""

        for run in para.runs:
            if not run.text:
                continue
            attrs: dict = {}
            if run.bold:
                attrs["bold"] = True
            if run.italic:
                attrs["italic"] = True
            if run.underline:
                attrs["underline"] = True
            if run.font.strike:
                attrs["strike"] = True
            if attrs:
                ops.append({"insert": run.text, "attributes": attrs})
            else:
                ops.append({"insert": run.text})

        nl_attrs: dict = {}
        if "heading 1" in style:
            nl_attrs["header"] = 1
        elif "heading 2" in style:
            nl_attrs["header"] = 2
        elif "heading 3" in style:
            nl_attrs["header"] = 3
        elif "list bullet" in style:
            nl_attrs["list"] = "bullet"
        elif "list number" in style:
            nl_attrs["list"] = "ordered"
        elif "block text" in style or "quote" in style:
            nl_attrs["blockquote"] = True

        if nl_attrs:
            ops.append({"insert": "\n", "attributes": nl_attrs})
        else:
            ops.append({"insert": "\n"})

    if not ops:
        ops = [{"insert": "\n"}]
    return json.dumps({"ops": ops})
