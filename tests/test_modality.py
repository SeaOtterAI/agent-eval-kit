"""Modality detection + artifact-part normalization."""

from __future__ import annotations

import base64

from agent_eval_kit import modality as M


def test_detect_text_vs_code():
    mod, parts = M.normalize("Hello, this is a plain sentence.")
    assert mod == "text" and parts[0]["text"]
    mod2, _ = M.normalize("def f(x):\n    import os\n    return os.path.join(x)")
    assert mod2 == "code"


def test_detect_data_url_image():
    payload = base64.b64encode(b"\x89PNG fake").decode()
    mod, parts = M.normalize(f"data:image/png;base64,{payload}")
    assert mod == "image"
    assert parts[0]["mime_type"] == "image/png" and parts[0]["data_b64"] == payload


def test_detect_file_path(tmp_path):
    p = tmp_path / "memo.md"
    p.write_text("# Memo\nbody")
    mod, parts = M.normalize(str(p))
    assert mod == "text" and "Memo" in parts[0]["text"] and parts[0]["logical_name"] == "memo.md"


def test_detect_http_url_and_bytes():
    mod, parts = M.normalize("https://example.com/clip.mp4")
    assert mod == "video" and parts[0]["uri"].endswith("clip.mp4")
    mod2, parts2 = M.normalize(b"\x00\x01\x02", mime="image/png")
    assert mod2 == "image" and parts2[0]["data_b64"]


def test_explicit_modality_wins():
    mod, _ = M.normalize("def x(): pass", modality="text")
    assert mod == "text"


def test_unknown_modality_falls_back_to_text():
    mod, _ = M.normalize("plain", modality="not-a-real-modality")
    assert mod == "text"


def test_multipart_list_uses_first_for_modality():
    payload = base64.b64encode(b"\x89PNG").decode()
    mod, parts = M.normalize([f"data:image/png;base64,{payload}", "a caption"])
    assert mod == "image" and len(parts) == 2
