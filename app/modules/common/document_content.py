"""校验教程原文件，并为 Word 和文本生成不执行脚本的阅读页面。"""

from base64 import b64encode
from html import escape
from io import BytesIO
from pathlib import PurePath, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException

DOCUMENT_LIMIT = 20 * 1024 * 1024
VIDEO_LIMIT = 100 * 1024 * 1024
FILE_TYPES = {
    ".pdf": ("document", "application/pdf"),
    ".docx": (
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".txt": ("document", "text/plain"),
    ".md": ("document", "text/markdown"),
    ".mp4": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
}
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def document_page(body: str) -> bytes:
    """用固定样式包裹已转义的正文，页面不包含脚本或外部资源。"""
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>教程阅读</title><style>body{font:16px/1.8 sans-serif;padding:24px;"
        "max-width:960px;margin:auto;color:#24344d}p,pre{white-space:pre-wrap;"
        "overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}"
        "td{border:1px solid #ddd;padding:8px;vertical-align:top}"
        "img{display:block;max-width:100%;height:auto;margin:12px 0}</style><body>"
        + body
        + "</body></html>"
    ).encode("utf-8")


def word_images(archive: ZipFile) -> dict[str, str]:
    """只读取文档内的 PNG/JPEG/GIF/WebP 插图，忽略外部链接及可执行图像格式。"""
    path = "word/_rels/document.xml.rels"
    if path not in archive.namelist():
        return {}
    content = archive.read(path)
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ValueError("关系文件包含不支持的声明")
    images = {}
    for relation in ET.fromstring(content):
        if relation.get("TargetMode") == "External" or not relation.get("Type", "").endswith(
            "/image"
        ):
            continue
        target = PurePosixPath("word") / relation.get("Target", "")
        if (
            target.is_absolute()
            or ".." in target.parts
            or not target.as_posix().startswith("word/media/")
        ):
            continue
        if target.as_posix() not in archive.namelist():
            continue
        data = archive.read(target.as_posix())
        media_type = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            media_type = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            media_type = "image/webp"
        if media_type:
            images[relation.get("Id", "")] = (
                f"data:{media_type};base64,{b64encode(data).decode('ascii')}"
            )
    return images


def word_preview(content: bytes) -> bytes:
    """读取受限大小的 DOCX 正文，保留段落、插图和表格，不访问外部资源或宏。"""
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(item.file_size for item in entries) > DOCUMENT_LIMIT:
                raise ValueError("文档解压后过大")
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError("不支持加密文档")
            document = archive.read("word/document.xml")
            if b"<!DOCTYPE" in document or b"<!ENTITY" in document:
                raise ValueError("文档包含不支持的声明")
            root = ET.fromstring(document)
            images = word_images(archive)
    except (BadZipFile, KeyError, ET.ParseError, ValueError, RuntimeError):
        raise HTTPException(422, "Word 文件无效、已加密或内容过大，请另存为 DOCX 或 PDF") from None

    image_bytes = 0

    def paragraph(node: ET.Element) -> str:
        nonlocal image_bytes
        parts = []
        for item in node.iter():
            if item.tag == WORD_NS + "t":
                parts.append(escape(item.text or ""))
            elif item.tag in (WORD_NS + "br", WORD_NS + "tab"):
                parts.append("\n")
            elif (
                item.tag.endswith("}blip") or item.tag == "{urn:schemas-microsoft-com:vml}imagedata"
            ):
                image = images.get(item.get(REL_NS + "embed") or item.get(REL_NS + "id"))
                if image:
                    image_bytes += len(image)
                    if image_bytes > DOCUMENT_LIMIT * 2:
                        raise HTTPException(422, "文档重复插图过多，请另存为 PDF 后上传")
                    parts.append(f'<img src="{image}" alt="文档插图">')
        return "<p>" + "".join(parts) + "</p>"

    body = root.find(WORD_NS + "body")
    if body is None:
        raise HTTPException(422, "Word 文件缺少正文")
    blocks = []
    for node in body:
        if node.tag == WORD_NS + "p":
            blocks.append(paragraph(node))
        elif node.tag == WORD_NS + "tbl":
            rows = []
            for row in node.findall(WORD_NS + "tr"):
                cells = [
                    "<td>" + "".join(paragraph(p) for p in cell.findall(WORD_NS + "p")) + "</td>"
                    for cell in row.findall(WORD_NS + "tc")
                ]
                rows.append("<tr>" + "".join(cells) + "</tr>")
            blocks.append("<table>" + "".join(rows) + "</table>")
    return document_page("".join(blocks))


def inspect_document(name: str, content: bytes) -> tuple[str, str, bytes | None]:
    """校验文件名、大小及实际内容，返回类型、媒体类型和可选阅读预览。"""
    if (
        not name.strip()
        or len(name) > 200
        or any(c in name for c in "/\\")
        or any(ord(c) < 32 for c in name)
    ):
        raise HTTPException(422, "文件名称无效，请重命名后上传")
    suffix = PurePath(name).suffix.lower()
    if suffix not in FILE_TYPES:
        raise HTTPException(422, "支持 PDF、DOCX、TXT、MD 文档及 MP4、WebM 视频")
    kind, media_type = FILE_TYPES[suffix]
    if not content:
        raise HTTPException(422, "不能上传空文件")
    if len(content) > (VIDEO_LIMIT if kind == "video" else DOCUMENT_LIMIT):
        raise HTTPException(413, "文档不能超过 20 MiB，视频不能超过 100 MiB")
    is_valid = True
    preview = None
    if suffix == ".pdf":
        is_valid = content.startswith(b"%PDF-") and b"%%EOF" in content[-4096:]
    elif suffix == ".mp4":
        is_valid = len(content) >= 16 and content[4:8] == b"ftyp"
    elif suffix == ".webm":
        is_valid = content.startswith(b"\x1aE\xdf\xa3") and b"webm" in content[:4096]
    elif suffix == ".docx":
        preview = word_preview(content)
    else:
        try:
            text = content.decode("utf-8-sig")
            is_valid = "\0" not in text
            preview = document_page("<pre>" + escape(text) + "</pre>")
        except UnicodeDecodeError:
            raise HTTPException(422, "文本教程请使用 UTF-8 编码后重新上传") from None
    if not is_valid:
        raise HTTPException(422, "文件内容与类型不符，请选择有效的文档或视频")
    return kind, media_type, preview
