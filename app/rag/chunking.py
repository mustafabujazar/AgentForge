"""Markdown-aware chunking for the AgentForge knowledge corpus.

Design goals:

* **Respect document structure.** Chunks never begin in the middle of a heading,
  table or fenced code block. Every chunk carries its heading breadcrumb
  (``Doc title > Section > Subsection``) so it is self-describing when retrieved.
* **Retrieval-sized.** Target ~400–800 characters per chunk (configured by
  ``Settings.rag_chunk_size``, default 700), with a small sentence overlap when a
  single paragraph has to be split.
* **Deterministic.** No randomness; output depends only on the input text and
  parameters.

The chunker is intentionally independent of the vector store and ORM so it can
be unit-tested on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.embeddings import split_sentences
from app.rag.types import Chunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(```|~~~)")
_LIST_RE = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+")

DEFAULT_CHUNK_SIZE = 700
DEFAULT_OVERLAP = 100
_MIN_BODY = 60
_PACK_FILL_RATIO = 0.55


@dataclass(slots=True)
class _Section:
    headings: tuple[str, ...]
    lines: list[str]

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.headings)


def split_sections(markdown_text: str) -> list[_Section]:
    """Split markdown into sections, tracking the heading hierarchy.

    Fenced code blocks are respected: a line starting with ``#`` inside a fence is
    code, not a heading.
    """
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[_Section] = []
    heading_stack: list[str] = []
    current_headings: list[str] = []
    current_lines: list[str] = []
    in_fence = False

    for line in lines:
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            current_lines.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(stripped)
        if match:
            sections.append(_Section(tuple(current_headings), current_lines))
            level = len(match.group(1))
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(match.group(2).strip())
            current_headings = list(heading_stack)
            current_lines = []
        else:
            current_lines.append(line)
    sections.append(_Section(tuple(current_headings), current_lines))
    return sections


def _split_blocks(lines: list[str]) -> list[str]:
    """Group section lines into atomic blocks: paragraphs, lists, tables, fences."""
    blocks: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                blocks.append(text)
            buffer.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            flush()
            fence = [line]
            index += 1
            while index < len(lines):
                fence.append(lines[index])
                if _FENCE_RE.match(lines[index].strip()):
                    index += 1
                    break
                index += 1
            blocks.append("\n".join(fence).strip())
            continue
        if not stripped:
            flush()
            index += 1
            continue
        if stripped.startswith("|"):  # keep markdown tables in one piece
            flush()
            table: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table.append(lines[index].rstrip())
                index += 1
            blocks.append("\n".join(table).strip())
            continue
        if _LIST_RE.match(stripped):  # keep a list together
            flush()
            items: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                candidate_stripped = candidate.strip()
                if not candidate_stripped:
                    break
                if _LIST_RE.match(candidate_stripped) or candidate.startswith(("  ", "\t")):
                    items.append(candidate.rstrip())
                    index += 1
                    continue
                break
            blocks.append("\n".join(items).strip())
            continue
        buffer.append(line.rstrip())
        index += 1
    flush()
    return [block for block in blocks if block]


def _word_windows(block: str, target: int, overlap: int) -> list[str]:
    """Split an over-long block on word boundaries with a small char overlap."""
    words = block.split()
    if not words:
        return []
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        if current and length + 1 + len(word) > target:
            pieces.append(" ".join(current))
            tail: list[str] = []
            tail_length = 0
            for previous in reversed(current):
                if tail and tail_length + 1 + len(previous) > overlap:
                    break
                tail.insert(0, previous)
                tail_length += len(previous) + 1
            current = tail
            length = tail_length
        current.append(word)
        length += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces


def _split_long_block(block: str, target: int, overlap: int) -> list[str]:
    """Split an over-long block on sentence boundaries (falling back to words)."""
    sentences = split_sentences(block)
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for sentence in sentences:
        if len(sentence) > target:
            if current:
                pieces.append(" ".join(current))
                current = []
                length = 0
            pieces.extend(_word_windows(sentence, target, overlap))
            continue
        if current and length + 1 + len(sentence) > target:
            pieces.append(" ".join(current))
            carry = current[-1] if len(current[-1]) <= overlap else ""
            current = [carry] if carry else []
            length = len(carry)
        current.append(sentence)
        length += len(sentence) + 1
    if current:
        pieces.append(" ".join(current))
    return [piece.strip() for piece in pieces if piece.strip()]


def _pack_blocks(blocks: list[str], target: int, overlap: int) -> list[str]:
    """Greedily pack atomic blocks into ~target-sized chunk bodies."""
    bodies: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if len(block) > target:
            if current:
                bodies.append("\n\n".join(current))
                current = []
                size = 0
            bodies.extend(_split_long_block(block, target, overlap))
            continue
        addition = len(block) + (2 if current else 0)
        if current and size + addition > target and size >= target * _PACK_FILL_RATIO:
            bodies.append("\n\n".join(current))
            carry = _tail_overlap(current, overlap)
            current = [carry] if carry else []
            size = len(carry)
            addition = len(block) + (2 if current else 0)
        current.append(block)
        size += addition
    if current:
        bodies.append("\n\n".join(current))
    return [body for body in bodies if body.strip()]


def _tail_overlap(blocks: list[str], overlap: int) -> str:
    """Return the trailing sentence(s) of the already-packed blocks (<= overlap)."""
    if overlap <= 0 or not blocks:
        return ""
    sentences: list[str] = []
    total = 0
    for block in reversed(blocks):
        for sentence in reversed(split_sentences(block)):
            if total + len(sentence) + 1 > overlap:
                return " ".join(sentences).strip()
            sentences.insert(0, sentence)
            total += len(sentence) + 1
    return " ".join(sentences).strip()


def _merge_tiny_bodies(bodies: list[str], target: int) -> list[str]:
    """Merge a tiny trailing chunk into its predecessor when it fits."""
    if len(bodies) < 2:
        return bodies
    merged: list[str] = []
    for body in bodies:
        if (
            merged
            and len(body) < _MIN_BODY
            and len(merged[-1]) + 2 + len(body) <= int(target * 1.6)
        ):
            merged[-1] = f"{merged[-1]}\n\n{body}"
        else:
            merged.append(body)
    return merged


def chunk_markdown(
    text: str,
    *,
    title: str = "",
    source: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split markdown into context-carrying, retrieval-sized chunks.

    Args:
        text: Full markdown document.
        title: Document title; used as breadcrumb fallback for preambles.
        source: Human-readable source label stored with each chunk.
        chunk_size: Target character budget for the body (heading prefix excluded).
        overlap: Character budget of sentence overlap when splitting long blocks.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    chunk_size = max(200, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size // 2))

    chunks: list[Chunk] = []
    running_index = 0
    for section in split_sections(text):
        blocks = _split_blocks(section.lines)
        if not blocks:
            continue
        body_pieces = _merge_tiny_bodies(_pack_blocks(blocks, chunk_size, overlap), chunk_size)
        breadcrumb = section.breadcrumb or (title or "Document")
        for body in body_pieces:
            chunk_text = f"{breadcrumb}\n\n{body}".strip()
            if len(chunk_text) <= len(breadcrumb):
                continue
            chunks.append(
                Chunk(
                    title=title or breadcrumb.split(" > ")[0],
                    source=source,
                    text=chunk_text,
                    chunk_index=running_index,
                    metadata={
                        "headings": list(section.headings),
                        "section": breadcrumb,
                        "char_length": len(chunk_text),
                    },
                )
            )
            running_index += 1
    return chunks


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "chunk_markdown",
    "split_sections",
]
