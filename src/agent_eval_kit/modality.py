"""Turn whatever an agent hands us into eval-API artifact parts + a modality.

An agent should be able to pass: a string of text/code, a file path, a ``data:``
URL, an ``http(s)`` URL, raw bytes, an already-shaped part dict, or a list mixing
those — and the kit figures out the modality and builds the ``artifact_parts`` the
eval API expects (``ArtifactPartRef`` shape:
``{mime_type, text?, data_b64?, uri?, logical_name?}``).
"""

from __future__ import annotations

import base64
import mimetypes
import os
from collections.abc import Iterable
from typing import Any

from .types import FileError

# The modalities the eval API understands.
MODALITIES = {
    "text", "code", "image", "deck", "spreadsheet", "document",
    "video", "audio", "outcome_metric", "video_transcript", "image_caption",
}

# Extension → modality. Drives auto-detection.
_EXT_MODALITY = {
    ".py": "code", ".js": "code", ".ts": "code", ".tsx": "code", ".jsx": "code",
    ".go": "code", ".rs": "code", ".java": "code", ".c": "code", ".cpp": "code",
    ".rb": "code", ".php": "code", ".sh": "code", ".sql": "code", ".diff": "code", ".patch": "code",
    ".md": "text", ".txt": "text", ".rst": "text",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".tiff": "image",
    ".pdf": "document", ".docx": "document", ".doc": "document", ".odt": "document",
    ".pptx": "deck", ".ppt": "deck", ".key": "deck",
    ".xlsx": "spreadsheet", ".xls": "spreadsheet", ".csv": "spreadsheet", ".tsv": "spreadsheet",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video", ".avi": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio", ".ogg": "audio", ".aac": "audio",
}

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME = {"application/json", "application/xml", "application/x-yaml", "application/yaml"}


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _mime_for(name: str, fallback: str = "application/octet-stream") -> str:
    guess, _ = mimetypes.guess_type(name)
    return guess or fallback


def _modality_for_mime(mime: str) -> str | None:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime in _TEXT_MIME or any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return "text"
    return None


def _looks_like_code(text: str) -> bool:
    markers = ("def ", "function ", "class ", "import ", "#include", "</", "{", "};", "=>", "::")
    hits = sum(1 for m in markers if m in text)
    return hits >= 2


def _looks_like_path(s: str) -> bool:
    """Path-SHAPED, regardless of whether the file exists on disk.

    A ``file://`` URL, an explicit fs prefix (``/``, ``./``, ``../``, ``~``), or
    a single-line bare-ish string with a known media/doc extension all *look*
    like a local file the caller meant to grade. Existence is checked
    separately so a missing ``report.pdf`` can be reported as an unreadable file
    instead of being silently graded as the literal filename text.
    """
    if s.startswith("file://"):
        return True
    if "\n" in s or len(s) > 1024:
        return False
    if s.startswith(("/", "./", "../", "~")):
        return True
    # bare-ish path with a known media/doc extension (e.g. ``report.pdf``).
    return _ext(s) in _EXT_MODALITY


def _is_path(s: str) -> bool:
    """A path-shaped string that ALSO resolves to a real local file.

    The happy path: a file that exists on disk → read it client-side. A
    path-shaped string with no file behind it is handled by the caller (it is
    an actionable :class:`FileError`, not silent fall-through to text)."""
    if not _looks_like_path(s):
        return False
    if s.startswith("file://"):
        return os.path.exists(os.path.expanduser(s[len("file://"):]))
    return os.path.exists(os.path.expanduser(s))


def _read_file_part(path: str) -> tuple[str, dict[str, Any]]:
    """Read a local file → (modality, part). Text files become a ``text`` part;
    binaries become a base64 ``data_b64`` part the eval API converts/renders."""
    if path.startswith("file://"):
        path = path[len("file://"):]
    path = os.path.expanduser(path)
    name = os.path.basename(path)
    ext = _ext(name)
    modality = _EXT_MODALITY.get(ext, "")
    mime = _mime_for(name)
    text_modality = modality in ("text", "code") or (_modality_for_mime(mime) == "text")
    try:
        if text_modality:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            return (modality or ("code" if _looks_like_code(body) else "text"),
                    {"mime_type": mime if mime != "application/octet-stream" else "text/plain",
                     "text": body, "logical_name": name})
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        # FileNotFoundError/PermissionError/etc. become a typed, actionable
        # error instead of a raw uncaught traceback — most often this is the
        # hosted critic being handed a path it cannot see.
        raise FileError(path, str(exc)) from exc
    return (modality or (_modality_for_mime(mime) or "document"),
            {"mime_type": mime, "data_b64": base64.b64encode(data).decode("ascii"),
             "logical_name": name})


def _data_url_part(url: str) -> tuple[str, dict[str, Any]]:
    # data:<mime>;base64,<payload>
    head, _, payload = url[len("data:"):].partition(",")
    mime = head.split(";", 1)[0] or "application/octet-stream"
    is_b64 = ";base64" in head
    if is_b64:
        part = {"mime_type": mime, "data_b64": payload}
    else:
        from urllib.parse import unquote
        part = {"mime_type": mime or "text/plain", "text": unquote(payload)}
    return (_modality_for_mime(mime) or "document"), part


def _one(work: Any, *, mime: str | None = None, name: str | None = None) -> tuple[str, dict[str, Any]]:
    """Normalize one item → (modality_guess, part)."""
    if isinstance(work, dict):
        # Already a part dict; trust its mime, guess modality.
        m = work.get("mime_type") or mime or "text/plain"
        guess = (_modality_for_mime(m)
                 or _EXT_MODALITY.get(_ext(work.get("logical_name") or name or ""), "")
                 or ("code" if work.get("text") and _looks_like_code(work["text"]) else "text"))
        return guess, work
    if isinstance(work, (bytes, bytearray)):
        m = mime or "application/octet-stream"
        return (_modality_for_mime(m) or "document"), {
            "mime_type": m, "data_b64": base64.b64encode(bytes(work)).decode("ascii"),
            **({"logical_name": name} if name else {})}
    s = str(work)
    if s.startswith("data:"):
        return _data_url_part(s)
    if s.startswith(("http://", "https://")):
        m = mime or _mime_for(s)
        return (_modality_for_mime(m) or _EXT_MODALITY.get(_ext(s), "") or "document"), {
            "mime_type": m, "uri": s, **({"logical_name": name} if name else {})}
    if _is_path(s):
        return _read_file_part(s)
    # Path-SHAPED but no file behind it: a bare ``report.pdf`` / ``pitch.pptx``
    # with a known media/doc extension, or an explicit ``file://`` ref, that
    # does not exist locally. Do NOT silently grade the filename as text — that
    # is the confirmed hosted-critic gap. Raise an actionable error instead.
    # (A plain fs-prefixed path like ``./notes`` with NO known extension is left
    #  to fall through to text, to keep arbitrary leading-slash strings working.)
    if s.startswith("file://") or (_looks_like_path(s) and _ext(s) in _EXT_MODALITY):
        raise FileError(s, "no such file (or not readable from here)")
    # plain inline text/code
    return ("code" if _looks_like_code(s) else "text"), {
        "mime_type": mime or "text/plain", "text": s,
        **({"logical_name": name} if name else {})}


def normalize(work: Any, *, modality: str | None = None,
              mime: str | None = None, name: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(modality, artifact_parts)`` for whatever ``work`` is.

    ``work`` may be a single item or a list of items. An explicit ``modality``
    wins; otherwise it's inferred from the first part. Always returns at least
    one part so the critic has content to ground against.
    """
    items: Iterable[Any]
    if isinstance(work, (list, tuple)):
        items = work
    else:
        items = [work]
    parts: list[dict[str, Any]] = []
    first_guess = "text"
    for i, item in enumerate(items):
        guess, part = _one(item, mime=mime, name=name)
        if i == 0:
            first_guess = guess
        parts.append(part)
    chosen = modality or first_guess
    if chosen not in MODALITIES:
        chosen = "text"
    return chosen, parts
