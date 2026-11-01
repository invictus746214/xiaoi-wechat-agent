"""Describe one image bubble with an OpenAI-compatible vision endpoint.

Only called when ``vision.enabled`` is true and the endpoint/model actually
accept images. Any failure returns "" so the caller skips the message instead
of inventing a description.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 8 * 1024 * 1024
LOG = logging.getLogger("wechat-autoreply")


class VisionError(Exception):
    pass


def build_payload(model, image_b64, prompt, max_tokens=200):
    images = image_b64 if isinstance(image_b64, list) else [image_b64]
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *({"type": "image_url", "image_url": {"url": "data:image/png;base64," + item}}
                      for item in images),
                ],
            }
        ],
        "max_tokens": int(max_tokens),
        "stream": False,
    }


def encode_image(path):
    with open(path, "rb") as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise VisionError("image_too_large")
    return base64.b64encode(data).decode("ascii")


def image_hash(path):
    """Average hash plus colour and aspect ratio for repeat sticker lookup."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    source = GdkPixbuf.Pixbuf.new_from_file(path)
    sample = source.scale_simple(8, 8, GdkPixbuf.InterpType.BILINEAR)
    pixels, stride, channels = sample.get_pixels(), sample.get_rowstride(), sample.get_n_channels()
    colors = [tuple(pixels[y * stride + x * channels:y * stride + x * channels + 3])
              for y in range(8) for x in range(8)]
    luminance = [(299 * r + 587 * g + 114 * b) // 1000 for r, g, b in colors]
    mean = sum(luminance) // len(luminance)
    bits = sum(1 << i for i, value in enumerate(luminance) if value >= mean)
    tint = tuple(sum(color[i] for color in colors) // (len(colors) * 16) for i in range(3))
    detail = source.scale_simple(32, 32, GdkPixbuf.InterpType.BILINEAR)
    return "%016x-%x%x%x-%d-%s" % (bits, *tint,
        round(source.get_width() * 10 / max(1, source.get_height())),
        hashlib.sha256(detail.get_pixels()).hexdigest()[:12])


def describe(cfg, path, api_key, timeout=None, prompt=None):
    """Return a short description, or "" when anything is off."""
    if not api_key:
        return ""
    try:
        payload = build_payload(
            cfg["model"], [encode_image(item) for item in path] if isinstance(path, list) else encode_image(path),
            prompt or cfg["prompt"], cfg.get("max_tokens", 200)
        )
        if urlsplit(cfg["url"]).hostname == "api.deepseek.com":
            # Hidden reasoning can exhaust a small caption budget before any caption is returned.
            payload["thinking"] = {"type": "disabled"}
    except (OSError, VisionError, KeyError):
        return ""
    request = Request(
        cfg["url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout or cfg.get("timeout", 30)) as response:
            body = json.loads(response.read())
        content = body["choices"][0]["message"]["content"]
    except HTTPError as exc:
        LOG.warning("vision_request_failed=http_%d", exc.code)
        return ""
    except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        LOG.warning("vision_request_failed=%s", type(exc).__name__)
        return ""
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    text = (content or "").strip()
    if not text:
        LOG.warning("vision_response_empty finish=%s", body["choices"][0].get("finish_reason", "unknown"))
    return text[:300]
