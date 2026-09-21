"""受限文档解析及字符分段；不执行宏、公式、外部链接或文档中的指令。"""

import re
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from defusedxml import ElementTree
from fastapi import HTTPException

from app.modules.knowledge.schemas import IndexInput

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARACTERS = 2_000_000
MAX_CHUNKS = 20000
ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".xls"}


def document_name(name: str) -> str:
    """剥离浏览器文件路径并校验格式；原文件名永不用于磁盘寻址。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
        raise HTTPException(422, "文档名称无效")
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "支持 PDF、DOCX、XLSX、XLS、TXT、Markdown；旧版 DOC 请先转为 DOCX")
    return name


def parse_document(name: str, content: bytes) -> str:
    """提取可检索文字；损坏、加密、扫描件和超限内容明确失败，不伪造分段。"""
    suffix = Path(name).suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = content.decode("gb18030")
            if "\x00" in text:
                raise ValueError("文档不是可读取的文本")
        elif suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                raise ValueError("请先移除 PDF 密码")
            parts = []
            count = 0
            for page in reader.pages:
                part = page.extract_text() or ""
                count += len(part)
                if count > MAX_TEXT_CHARACTERS:
                    raise ValueError("解析文字超过 200 万字符，请拆分文档")
                parts.append(part)
            text = "\n\n".join(parts)
        elif suffix == ".xls":
            import xlrd

            book = xlrd.open_workbook(file_contents=content, on_demand=True)
            try:
                parts = []
                count = 0
                for sheet in book.sheets():
                    for row in range(sheet.nrows):
                        part = "\t".join(str(value) for value in sheet.row_values(row))
                        count += len(part)
                        if count > MAX_TEXT_CHARACTERS:
                            raise ValueError("解析文字超过 200 万字符，请拆分文档")
                        parts.append(part)
                text = "\n".join(parts)
            finally:
                book.release_resources()
        else:
            text = parse_office(content, suffix)
        text = text.strip()
        if not text:
            raise ValueError("文档没有可提取文字，扫描件请先完成 OCR")
        if len(text) > MAX_TEXT_CHARACTERS:
            raise ValueError("解析文字超过 200 万字符，请拆分文档")
        return text
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except Exception:
        # 解析器异常可能包含内部路径，用户只接收稳定的业务说明。
        raise HTTPException(422, "文档解析失败，请检查格式、密码或文件完整性") from None


def parse_office(content: bytes, suffix: str) -> str:
    """读取 OOXML 正文与缓存单元格值，先限制解压体积以防压缩包耗尽内存。"""
    with ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_FILE_BYTES:
            raise ValueError("文档解压后超过处理限制，请拆分文档")
        if suffix == ".docx":
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            root = ElementTree.fromstring(archive.read("word/document.xml"))
            return "\n".join(
                "".join(node.text or "" for node in paragraph.findall(".//w:t", ns))
                for paragraph in root.findall(".//w:p", ns)
            )
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = [
                "".join(node.text or "" for node in item.findall(".//s:t", ns)) for item in root
            ]
        rows = []
        for entry in entries:
            if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", entry.filename):
                continue
            root = ElementTree.fromstring(archive.read(entry))
            for row in root.findall(".//s:row", ns):
                values = []
                for cell in row.findall("s:c", ns):
                    value = cell.findtext("s:v", default="", namespaces=ns)
                    if cell.get("t") == "s" and value:
                        value = strings[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = "".join(node.text or "" for node in cell.findall(".//s:t", ns))
                    values.append(value)
                rows.append("\t".join(values))
        return "\n".join(rows)


def split_text(text: str, config: IndexInput) -> list[str]:
    """按字符上限切分；段落和自动模式优先使用边界，重叠不会阻止游标前进。"""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + config.chunk_size, len(text))
        if end < len(text) and config.chunk_mode != "固定长度":
            delimiters = (
                ("\n",)
                if config.chunk_mode in ("按段落", "规则分段")
                else ("\n", "。", "！", "？", ". ")
            )
            boundary = max(
                text.rfind(mark, start + config.chunk_size // 2, end) for mark in delimiters
            )
            if boundary > start + config.chunk_overlap:
                end = boundary + 1
        part = text[start:end].strip()
        if part:
            chunks.append(part)
        if len(chunks) > MAX_CHUNKS:
            raise HTTPException(422, "分段数量超过处理限制，请拆分文档或增加分段大小")
        if end == len(text):
            break
        start = end - config.chunk_overlap
    return chunks
