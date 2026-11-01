"""Configuration loading for the WeChat auto-reply bridge.

Every option is conservative: a missing or broken config file must fall back to
the behaviour that was already deployed, never to something more chatty.
"""
from __future__ import annotations

import copy
import json
import os
import math

DEFAULTS = {
    "api": {
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
        "key_file": "/etc/wechat-autoreply.env",
        "timeout": 45,
        "max_tokens": 256,
    },
    "persona_file": "persona.md",
    "memory": {
        "enabled": True,
        "recent_turns": 40,
        "max_facts": 20,
        "retention_days": 14,
        "summary_every": 60,
        "replay_minutes": 15,
        "db": "state.db",
        "scene_state_enabled": False,
        "semantic_enabled": False,
    },
    "metrics": {
        "enabled": True,
        "path": "/var/lib/wechat-autoreply/metrics.jsonl",
        "days": 7,
    },
    "ocr": {
        "enabled": True,
        "image_enabled": True,
        "lang": "chi_sim+eng",
        "timeout": 8,
    },
    "runtime": {
        "database_pipeline": False,
        "config_reload_seconds": 10,
        "purge_check_minutes": 30,
        "reply_unread_on_start": False,
        "catch_up_on_start": True,
    },
    "tools": {"enabled": True, "max_rounds": 2},
    "policy": {
        "quiet_hours": [],
        "group_mention_only": False,
        "group_mention_reply": True,
        "group_self_name": "",
        "group_self_names": [],
        "group_always_reply": [],
        "group_engaged_seconds": 300,
        "group_interject_enabled": True,
        "group_interject_probability": 0.08,
        "group_interject_min_seconds": 600,
        "group_check_min_seconds": 30,
        "whitelist": [],
        "skip_contacts": ["Weixin Team", "Tencent News", "File Transfer", "文件传输助手"],
        "daily_reply_cap": 0,
    },
    "proactive": {
        "enabled": False,
        "group_interjections": False,
        "greetings": [],
        "heartbeat_minutes": 30,
        "webhook": "",
    },
    "vision": {
        "enabled": False,
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
        "key_file": "",
        "timeout": 30,
        "max_tokens": 600,
        "prompt": "用一句中文描述这张微信图片里的内容，看不清就说看不清。",
    },
    "moments": {
        "enabled": False,
        "post_at": [],
        "scan_minutes": 30,
        "scan_pages": 1,
        "interaction_enabled": True,
        "interaction_minutes": 10,
        "max_posts": 10,
        "dry_run": True,
        "authors": [],
        "skip_authors": [],
        "skip_keywords": [],
        "aliases": [],
        "quiet_hours": [],
        "like_enabled": False,
        "comment_enabled": False,
        "like_daily_cap": 20,
        "comment_daily_cap": 5,
        "comment_author_interval_seconds": 21600,
        "comment_repeat_days": 7,
        "min_interval_seconds": 20,
    },
    "dispatch": {"workers": 2, "max_outbox": 64},
    "quality": {"critic_enabled": False,"structured_reply":False,"intelligence_enabled":False},
    "tasks": {"enabled":False},
    "learning": {"enabled":False},
    "storage": {
        "db_reader_enabled": True,
        "wcdb_keys": "/etc/wechat-autoreply/wcdb-keys.json",
        "db_cache": "/var/lib/wechat-autoreply/db-cache",
    },
    "voice": {
        "enabled": True,
        "style": "playful",
        "max_chars": 140,
        "allow_private_only": False,
    },
    "relay": {"enabled": False, "allowed_titles": []},
    "stickers": {"enabled": False, "auto_send": False, "library_dir": "/var/lib/wechat-autoreply/stickers", "send_probability":.3,"min_interval_seconds":600,"same_sticker_interval_seconds":86400,"passive_collect":False,"prepare_before_reply":False,"capabilities_enabled":False},
    "delivery": {
        "split_enabled": True,
        "split_threshold": 25,
        "split_target": 30,
        "split_max": 3,
        "quota_enabled": True,
        "max_per_minute": 6,
        "max_per_10min": 30,
        "private_min_interval_seconds": 1,
        "group_min_interval_seconds": 15,
    },
}


def _merge(base, extra):
    result = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _positive_int(value, fallback, minimum=1, maximum=600):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _probability(value,fallback):
    try:number=float(value)
    except (TypeError,ValueError):return fallback
    return max(0.0,min(1.0,number)) if math.isfinite(number) else fallback


def _string_list(value):
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _greeting_list(value):
    items = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        at = str(entry.get("at", "")).strip()
        contact = str(entry.get("contact", "")).strip()
        text = str(entry.get("text", "")).strip()
        if at and contact and text:
            items.append({"at": at, "contact": contact, "text": text})
    return items


def normalize(raw):
    """Merge user config over defaults and coerce the fields we rely on."""
    config = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
    config["api"]["timeout"] = _positive_int(config["api"].get("timeout"), 45, 5, 120)
    config["api"]["max_tokens"] = _positive_int(config["api"].get("max_tokens"), 256, 32, 2048)
    config["memory"]["recent_turns"] = _positive_int(config["memory"].get("recent_turns"), 40, 0, 80)
    config["memory"]["max_facts"] = _positive_int(config["memory"].get("max_facts"), 20, 0, 100)
    config["memory"]["retention_days"] = _positive_int(config["memory"].get("retention_days"), 14, 1, 365)
    config["memory"]["summary_every"] = _positive_int(config["memory"].get("summary_every"), 60, 0, 400)
    config["memory"]["replay_minutes"] = _positive_int(config["memory"].get("replay_minutes"), 15, 0, 1440)
    config["metrics"]["days"] = _positive_int(config["metrics"].get("days"), 7, 1, 90)
    config["ocr"]["timeout"] = _positive_int(config["ocr"].get("timeout"), 8, 1, 60)
    config["ocr"]["image_enabled"] = bool(config["ocr"].get("image_enabled", True))
    if not isinstance(config["ocr"].get("lang"), str) or not config["ocr"]["lang"].strip():
        config["ocr"]["lang"] = "chi_sim+eng"
    config["runtime"]["config_reload_seconds"] = _positive_int(
        config["runtime"].get("config_reload_seconds"), 10, 0, 3600
    )
    config["runtime"]["purge_check_minutes"] = _positive_int(
        config["runtime"].get("purge_check_minutes"), 30, 0, 1440
    )
    config["runtime"]["reply_unread_on_start"] = bool(
        config["runtime"].get("reply_unread_on_start", False)
    )
    config["runtime"]["catch_up_on_start"] = bool(
        config["runtime"].get("catch_up_on_start", True)
    )
    config["tools"]["max_rounds"] = _positive_int(config["tools"].get("max_rounds"), 2, 0, 5)
    config["policy"]["daily_reply_cap"] = _positive_int(config["policy"].get("daily_reply_cap"), 0, 0, 100000)
    config["policy"]["whitelist"] = _string_list(config["policy"].get("whitelist"))
    config["policy"]["skip_contacts"] = _string_list(config["policy"].get("skip_contacts"))
    config["policy"]["group_mention_only"] = bool(config["policy"].get("group_mention_only"))
    config["policy"]["group_mention_reply"] = bool(config["policy"].get("group_mention_reply", True))
    config["policy"]["group_self_name"] = str(config["policy"].get("group_self_name") or "").strip()[:40]
    names = _string_list(config["policy"].get("group_self_names"))
    if config["policy"]["group_self_name"]:
        names.append(config["policy"]["group_self_name"])
    config["policy"]["group_self_names"] = list(dict.fromkeys(names))
    config["policy"]["group_always_reply"] = _string_list(config["policy"].get("group_always_reply"))
    config["policy"]["group_engaged_seconds"] = _positive_int(
        config["policy"].get("group_engaged_seconds"), 300, 0, 3600
    )
    config["policy"]["group_interject_enabled"] = bool(config["policy"].get("group_interject_enabled", True))
    config['policy']['group_check_min_seconds']=_positive_int(config['policy'].get('group_check_min_seconds'),30,3,3600)
    config['stickers']['send_probability']=_probability(config['stickers'].get('send_probability'),.3)
    config['stickers']['min_interval_seconds']=_positive_int(config['stickers'].get('min_interval_seconds'),600,10,86400)
    config['stickers']['same_sticker_interval_seconds']=_positive_int(config['stickers'].get('same_sticker_interval_seconds'),86400,60,86400)
    try:
        probability = float(config["policy"].get("group_interject_probability", 0.08))
    except (TypeError, ValueError):
        probability = 0.08
    config["policy"]["group_interject_probability"] = max(0.0, min(1.0, probability))
    config["policy"]["group_interject_min_seconds"] = _positive_int(
        config["policy"].get("group_interject_min_seconds"), 600, 0, 86400
    )
    config["proactive"]["group_interjections"] = bool(config["proactive"].get("group_interjections", False))
    config["proactive"]["heartbeat_minutes"] = _positive_int(
        config["proactive"].get("heartbeat_minutes"), 30, 0, 1440
    )
    config["proactive"]["greetings"] = _greeting_list(config["proactive"].get("greetings"))
    config["moments"]["post_at"] = _string_list(config["moments"].get("post_at"))
    config["moments"]["authors"] = _string_list(config["moments"].get("authors"))
    config["moments"]["skip_authors"] = _string_list(config["moments"].get("skip_authors"))
    config["moments"]["skip_keywords"] = _string_list(config["moments"].get("skip_keywords"))
    config["moments"]["aliases"] = _string_list(config["moments"].get("aliases"))
    config["moments"]["scan_minutes"] = _positive_int(config["moments"].get("scan_minutes"), 30, 1, 1440)
    config["moments"]["scan_pages"] = _positive_int(config["moments"].get("scan_pages"), 1, 1, 10)
    config["moments"]["interaction_enabled"] = bool(config["moments"].get("interaction_enabled", True))
    config["moments"]["interaction_minutes"] = _positive_int(
        config["moments"].get("interaction_minutes"), 10, 0, 1440
    )
    config["moments"]["max_posts"] = _positive_int(config["moments"].get("max_posts"), 10, 1, 50)
    config["moments"]["like_daily_cap"] = _positive_int(config["moments"].get("like_daily_cap"), 20, 0, 500)
    config["moments"]["comment_daily_cap"] = _positive_int(config["moments"].get("comment_daily_cap"), 5, 0, 100)
    config["moments"]["comment_author_interval_seconds"] = _positive_int(config["moments"].get("comment_author_interval_seconds"), 21600, 0, 604800)
    config["moments"]["comment_repeat_days"] = _positive_int(config["moments"].get("comment_repeat_days"), 7, 1, 30)
    config["moments"]["min_interval_seconds"] = _positive_int(
        config["moments"].get("min_interval_seconds"), 20, 0, 3600
    )
    config["moments"]["dry_run"] = bool(config["moments"].get("dry_run", True))
    config["vision"]["timeout"] = _positive_int(config["vision"].get("timeout"), 30, 5, 120)
    config["vision"]["max_tokens"] = _positive_int(config["vision"].get("max_tokens"), 600, 128, 2048)
    config["dispatch"]["workers"] = _positive_int(config["dispatch"].get("workers"), 2, 0, 8)
    config["dispatch"]["max_outbox"] = _positive_int(config["dispatch"].get("max_outbox"), 64, 4, 512)
    config["quality"]["critic_enabled"] = bool(config["quality"].get("critic_enabled", False))
    config["storage"]["db_reader_enabled"] = bool(config["storage"].get("db_reader_enabled", True))
    config["storage"]["wcdb_keys"] = str(config["storage"].get("wcdb_keys") or "/etc/wechat-autoreply/wcdb-keys.json")
    config["storage"]["db_cache"] = str(config["storage"].get("db_cache") or "/var/lib/wechat-autoreply/db-cache")
    config["voice"]["enabled"] = bool(config["voice"].get("enabled", True))
    config["voice"]["style"] = str(config["voice"].get("style") or "playful")
    config["voice"]["max_chars"] = int(config["voice"].get("max_chars", 140))
    config["voice"]["allow_private_only"] = bool(config["voice"].get("allow_private_only", False))
    config["delivery"]["quota_enabled"] = bool(config["delivery"].get("quota_enabled", True))
    config["delivery"]["split_enabled"] = bool(config["delivery"].get("split_enabled", True))
    for key, default, minimum, maximum in (("split_threshold",25,10,300), ("split_target",30,10,150), ("split_max",3,1,3)):
        config["delivery"][key] = _positive_int(config["delivery"].get(key), default, minimum, maximum)
    config["delivery"]["max_per_minute"] = _positive_int(config["delivery"].get("max_per_minute"), 6, 1, 60)
    config["delivery"]["max_per_10min"] = _positive_int(config["delivery"].get("max_per_10min"), 30, 1, 600)
    config["delivery"]["private_min_interval_seconds"] = _positive_int(
        config["delivery"].get("private_min_interval_seconds"), 1, 0, 60
    )
    config["delivery"]["group_min_interval_seconds"] = _positive_int(
        config["delivery"].get("group_min_interval_seconds"), 15, 0, 600
    )
    config["relay"]["enabled"] = bool(config["relay"].get("enabled", False))
    config["relay"]["allowed_titles"] = _string_list(config["relay"].get("allowed_titles"))
    return config


def load(path=None):
    """Load JSON config; unreadable or invalid files fall back to defaults."""
    if not path:
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        path = candidate if os.path.exists(candidate) else None
    if not path:
        return normalize({})
    try:
        with open(path, encoding="utf-8-sig") as source:
            return normalize(json.load(source))
    except (OSError, ValueError):
        return normalize({})


def read_api_key(path):
    """Read DEEPSEEK_API_KEY from an env-style file without sourcing it."""
    try:
        with open(path, encoding="utf-8") as source:
            for line in source:
                name, separator, value = line.rstrip("\r\n").partition("=")
                if name == "DEEPSEEK_API_KEY" and separator:
                    return value.strip()
    except OSError:
        pass
    return ""


def load_persona(path, fallback):
    try:
        with open(path, encoding="utf-8") as source:
            text = source.read().strip()
    except OSError:
        return fallback
    return text or fallback
