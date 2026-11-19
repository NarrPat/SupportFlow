"""UTF-8 normalization and reproducible, offset-preserving Markdown/TXT chunks."""
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from .contracts import CHUNKER_VERSION, KnowledgeError, sha256


MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_TOKENS = 450
OVERLAP_TOKENS = 60
HEADING = re.compile(r"^#{1,6}\s+(.+?)(?:\s+\{#([A-Za-z0-9_.-]{1,100})\})?\s*$")


def normalize(raw, filename):
    if not isinstance(raw, bytes) or len(raw) > MAX_UPLOAD_BYTES:
        raise KnowledgeError("UPLOAD_TOO_LARGE")
    media_type = Path(filename).suffix.lower().removeprefix(".")
    if media_type not in {"md", "txt"}:
        raise KnowledgeError("INVALID_DOCUMENT")
    try:
        text = raw.decode("utf-8-sig", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise KnowledgeError("INVALID_DOCUMENT") from None
    if not text.strip() or any(ord(c) < 32 and c not in "\n\t" for c in text) or "\x7f" in text:
        raise KnowledgeError("INVALID_DOCUMENT")
    return {"text": text, "media_type": media_type, "raw_sha256": sha256(raw), "content_sha256": sha256(text)}


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    start_offset: int
    text: str


def sections(text, media_type):
    lines = text.splitlines(keepends=True)
    boundaries, offset = [], 0
    for number, line in enumerate(lines):
        heading = HEADING.fullmatch(line.rstrip("\n")) if media_type == "md" else None
        if heading:
            boundaries.append((number, offset, heading[1], heading[2]))
        offset += len(line)
    if not boundaries or boundaries[0][0] != 0:
        boundaries.insert(0, (0, 0, "正文", "body"))
    seen = set()
    result = []
    for i, (line_number, offset, title, explicit) in enumerate(boundaries):
        next_line = boundaries[i+1][0] if i+1 < len(boundaries) else len(lines)
        selected = lines[line_number:next_line]
        while selected and not selected[-1].strip():
            selected.pop()
        body = "".join(selected).removesuffix("\n")
        if not body.strip():
            continue
        section_id = explicit or "heading-" + sha256(title)[:16]
        if section_id in seen:
            raise KnowledgeError("DUPLICATE_SECTION")
        seen.add(section_id)
        result.append(Section(section_id, title, offset, body))
    return result


def chunk_document(text, media_type, *, document_id, version_id, tokenizer):
    """Token offsets refer to original normalized text, never tokenizer.decode()."""
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    result = []
    for section in sections(text, media_type):
        encoded = tokenizer.encode(section.text)
        offsets = [pair for pair in encoded.offsets if pair[1] > pair[0]]
        if not offsets:
            continue
        start = 0
        paragraph_ends = [m.start() for m in re.finditer(r"\n\s*\n", section.text)]
        while start < len(offsets):
            end = min(start + MAX_TOKENS, len(offsets))
            if end < len(offsets):
                # Prefer a paragraph boundary, but avoid a fragment shorter than
                # the overlap that could fail to make progress.
                possible = [pos for pos in paragraph_ends
                            if offsets[min(start+OVERLAP_TOKENS, len(offsets)-1)][1] < pos <= offsets[end-1][1]]
                if possible:
                    boundary = possible[-1]
                    end = max(start+1, sum(pair[1] <= boundary for pair in offsets))
            char_start = 0 if start == 0 else offsets[start][0]
            char_end = len(section.text) if end == len(offsets) else offsets[end-1][1]
            body = section.text[char_start:char_end]
            count = len(tokenizer.encode(body).ids)
            while count > MAX_TOKENS and end > start+1:
                end -= 1
                char_end = offsets[end-1][1]
                body = section.text[char_start:char_end]
                count = len(tokenizer.encode(body).ids)
            if not 1 <= count <= MAX_TOKENS:
                raise KnowledgeError("INVALID_TOKENIZATION")
            absolute_start, absolute_end = section.start_offset+char_start, section.start_offset+char_end
            identity = ":".join((document_id, version_id, section.section_id, str(absolute_start),
                                 str(absolute_end), tokenizer.version, CHUNKER_VERSION))
            result.append({"evidence_id": str(uuid5(NAMESPACE_URL, "supportflow:chunk:"+identity)),
                "document_id": document_id, "version_id": version_id, "section_id": section.section_id,
                "title": section.title, "start_line": bisect_right(line_starts, absolute_start),
                "end_line": bisect_right(line_starts, absolute_end-1), "start_offset": absolute_start,
                "end_offset": absolute_end, "text": body, "content_sha256": sha256(body),
                "section_content_sha256": sha256(section.text), "token_count": count,
                "tokenizer_version": tokenizer.version, "chunker_version": CHUNKER_VERSION})
            if end == len(offsets):
                break
            start = max(start+1, end-OVERLAP_TOKENS)
    if not result:
        raise KnowledgeError("EMPTY_INDEX")
    return result
