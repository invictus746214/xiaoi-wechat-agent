"""OCR used as a second opinion when the accessibility tree is not enough.

The left conversation list is refreshed slower than the message pane and can be
distorted by drafts, so a screenshot crop is read with tesseract and compared
against the message text. It only runs on the fallback path, never on every poll.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter

CLEAN = re.compile(r"\s+")


def normalize(text):
    return CLEAN.sub("", text or "").strip().casefold()


def similarity(left, right):
    """Character-multiset overlap in [0, 1]; tolerant to OCR noise."""
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return round(min(len(a), len(b)) / max(len(a), len(b)), 3)
    common = sum((Counter(a) & Counter(b)).values())
    return round(common / max(len(a), len(b)), 3)


def _strip_ellipsis(text):
    return normalize(text).rstrip("…").rstrip(".")


def matches_preview(message, preview, minimum=6, threshold=0.6):
    """True when the truncated list preview plausibly comes from the message."""
    a, b = _strip_ellipsis(message), _strip_ellipsis(preview)
    if not a or not b:
        return False
    if len(b) >= minimum and (a.startswith(b) or b.startswith(a)):
        return True
    return similarity(message, preview) >= threshold


class Ocr:
    def __init__(self, binary="tesseract", lang="chi_sim+eng", timeout=8, enabled=True):
        self.binary = binary
        self.lang = lang
        self.timeout = timeout
        self.enabled = bool(enabled)

    def available(self):
        return self.enabled and bool(shutil.which(self.binary))

    def read(self, path):
        if not self.available():
            return ""
        try:
            result = subprocess.run(
                [self.binary, path, "stdout", "-l", self.lang, "--psm", "6"],
                capture_output=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or b"").decode("utf-8", "replace").strip()

    def words(self, path):
        """Word boxes from tesseract TSV: [(text, left, top, width, height)]."""
        if not self.available():
            return []
        try:
            result = subprocess.run(
                [self.binary, path, "stdout", "-l", self.lang, "--psm", "6", "tsv"],
                capture_output=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        words = []
        for line in (result.stdout or b"").decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12 or parts[0] != "5":
                continue
            text = parts[11].strip()
            if not text:
                continue
            try:
                left, top, width, height = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]))
            except ValueError:
                continue
            words.append((text, left, top, width, height))
        return words
