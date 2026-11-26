#!/usr/bin/env python3
"""Core recommendation, learning, rules, approval, move, and undo engine for Doxie Filer."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "doxie-scanner"
STATE_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "doxie-scanner"
ENV_FILE = CONFIG_DIR / "env"
PROFILE_FILE = Path(os.environ.get("DOXIE_PROFILE_FILE") or (CONFIG_DIR / "profile.json"))
HISTORY_FILE = STATE_DIR / "filing-history.jsonl"
CACHE_FILE = STATE_DIR / "rec-cache.json"
RULES_FILE = STATE_DIR / "filing-rules.json"
LMS_BIN = Path(os.environ.get("LMS_BIN") or (Path.home() / ".lmstudio" / "bin" / "lms"))

DEFAULT_INBOX_DIR = Path.home() / "Scans"
DEFAULT_TARGET_ROOT = Path.home() / "Documents"
DEFAULT_LMSTUDIO_URL = "http://127.0.0.1:1234"
DEFAULT_MODEL_FAST = "qwen/qwen3.5-4b"
DEFAULT_MODEL_STRONG = "qwen/qwen3.8-27b"
DEFAULT_FILER_LISTEN = "127.0.0.1:8401"

BASE_CUE_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "have", "will",
    "are", "was", "were", "been", "being", "not", "but", "you", "all", "can",
    "may", "our", "any", "has", "had", "its", "who", "which", "their", "them",
    "these", "those", "such", "than", "then", "into", "over", "under", "about",
    "other", "more", "some", "only", "also", "when", "where", "what", "please",
    "date", "page", "name", "address", "phone", "total", "number", "amount",
    "document", "scanned", "copy", "information", "statement", "notice", "form",
}

_CACHE_LOCK = threading.Lock()
_RULES_LOCK = threading.Lock()
_LLM_LOCK = threading.Lock()
_HASH_MEMO: dict[tuple[str, int, int], str] = {}


def load_env_file(env_file: Path = ENV_FILE) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not env_file.is_file():
        return vals
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def load_profile(profile_file: Path | None = None) -> dict[str, Any]:
    path = profile_file or Path(os.environ.get("DOXIE_PROFILE_FILE") or PROFILE_FILE)
    default_profile: dict[str, Any] = {
        "household_description": "a personal/family document archive",
        "people": {},
        "fallback_folder": "Unsorted",
        "extra_stopwords": [],
        "top_level_notes": [],
        "prune_subtrees": ["Scans", "Camera Uploads"],
        "conditional_deep": {},
        "deep_include_roots": {},
        "year_pruned_roots": ["Taxes"],
        "hide_in_browser": ["Scans"],
    }
    if not path.is_file():
        return default_profile
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            default_profile.update(loaded)
    except Exception:
        pass
    return default_profile


def get_config() -> dict[str, Any]:
    env = load_env_file()
    inbox = Path(
        os.environ.get("DOXIE_INBOX_DIR")
        or env.get("DOXIE_INBOX_DIR")
        or env.get("DOXIE_PDF_DIR")
        or DEFAULT_INBOX_DIR
    ).expanduser()
    target = Path(
        os.environ.get("DOXIE_TARGET_ROOT")
        or env.get("DOXIE_TARGET_ROOT")
        or DEFAULT_TARGET_ROOT
    ).expanduser()
    lms_url = (
        os.environ.get("DOXIE_LMSTUDIO_URL")
        or env.get("DOXIE_LMSTUDIO_URL")
        or DEFAULT_LMSTUDIO_URL
    ).rstrip("/")
    model_fast = os.environ.get("DOXIE_FILER_MODEL_FAST") or env.get("DOXIE_FILER_MODEL_FAST") or DEFAULT_MODEL_FAST
    model_strong = os.environ.get("DOXIE_FILER_MODEL_STRONG") or env.get("DOXIE_FILER_MODEL_STRONG") or DEFAULT_MODEL_STRONG
    listen = os.environ.get("DOXIE_FILER_LISTEN_ADDR") or env.get("DOXIE_FILER_LISTEN_ADDR") or DEFAULT_FILER_LISTEN
    ctx_len = int(os.environ.get("DOXIE_FILER_CONTEXT_LEN") or env.get("DOXIE_FILER_CONTEXT_LEN") or "16384")
    scanner_url = os.environ.get("DOXIE_URL") or env.get("DOXIE_LISTEN_ADDR") or "127.0.0.1:8400"
    if not scanner_url.startswith(("http://", "https://")):
        scanner_url = f"http://{scanner_url.lstrip(':') if not scanner_url.startswith(':') else '127.0.0.1' + scanner_url}"
    profile_path = Path(
        os.environ.get("DOXIE_PROFILE_FILE")
        or env.get("DOXIE_PROFILE_FILE")
        or PROFILE_FILE
    ).expanduser()
    return {
        "inbox_dir": inbox,
        "target_root": target,
        "lmstudio_url": lms_url,
        "model_fast": model_fast,
        "model_strong": model_strong,
        "listen_addr": listen,
        "context_length": ctx_len,
        "scanner_url": scanner_url.rstrip("/"),
        "profile": load_profile(profile_path),
    }


def cue_stopwords(profile: dict[str, Any] | None = None) -> set[str]:
    extra = {str(w).lower() for w in ((profile or {}).get("extra_stopwords") or []) if w}
    return BASE_CUE_STOPWORDS | extra


def person_aliases(profile: dict[str, Any] | None = None) -> dict[str, tuple[str, ...]]:
    raw = (profile or {}).get("people") or {}
    out: dict[str, tuple[str, ...]] = {}
    for canonical, aliases in raw.items():
        if isinstance(aliases, list):
            out[str(canonical).lower()] = tuple(str(a).lower() for a in aliases if a)
        elif isinstance(aliases, str):
            out[str(canonical).lower()] = (aliases.lower(),)
    return out


def list_inbox_pdfs(inbox_dir: Path) -> list[dict[str, Any]]:
    if not inbox_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for p in sorted(inbox_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if (
            not p.is_file()
            or p.suffix.lower() != ".pdf"
            or p.name.startswith(".")
            or p.name.endswith(".!sync")
            or p.name.endswith(".rsls")
        ):
            continue
        st = p.stat()
        pages = get_pdf_page_count(p)
        items.append(
            {
                "name": p.name,
                "path": str(p),
                "sizeBytes": st.st_size,
                "modifiedAt": dt.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(),
                "pageCount": pages,
            }
        )
    return items


def get_pdf_page_count(pdf_path: Path) -> int | None:
    if not shutil.which("pdfinfo"):
        return None
    proc = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=False)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def extract_doc_cues(text: str, max_terms: int = 20, profile: dict[str, Any] | None = None) -> list[str]:
    if not text:
        return []
    stops = cue_stopwords(profile)
    words = re.findall(r"\b[A-Za-z][A-Za-z0-9&-]{2,}\b", text)
    counts: dict[str, int] = {}
    for w in words:
        lw = w.lower()
        if lw in stops or lw.isdigit():
            continue
        weight = 3 if (w.isupper() and 2 <= len(w) <= 6) else 1
        counts[lw] = counts.get(lw, 0) + weight
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:max_terms]]


def load_rules() -> list[dict[str, Any]]:
    with _RULES_LOCK:
        if not RULES_FILE.is_file():
            return []
        try:
            data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def save_rules(rules: list[dict[str, Any]]) -> None:
    with _RULES_LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RULES_FILE.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, RULES_FILE)


def add_rule(
    name: str,
    folder: str,
    when: str,
    keywords: list[str] | None = None,
    person: str | None = None,
    source: str = "user",
) -> dict[str, Any]:
    clean_folder = sanitize_relative_folder(folder)
    if not clean_folder:
        raise ValueError("Rule folder cannot be empty")
    clean_name = (name or "").strip() or clean_folder.split("/")[-1]
    clean_when = (when or "").strip() or clean_name
    clean_kws = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
    clean_person = (person or "").strip() or None

    rules = load_rules()
    rule_id = (
        "rule-"
        + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-"
        + hashlib.sha1((clean_name + clean_folder).encode()).hexdigest()[:5]
    )
    rule = {
        "id": rule_id,
        "name": clean_name,
        "when": clean_when,
        "keywords": clean_kws,
        "person": clean_person,
        "folder": clean_folder,
        "enabled": True,
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "source": source,
    }
    rules.insert(0, rule)
    save_rules(rules)
    invalidate_cache(clear_all=True)
    return rule


def update_rule(rule_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    rules = load_rules()
    for r in rules:
        if r.get("id") == rule_id:
            if "name" in patch and str(patch["name"]).strip():
                r["name"] = str(patch["name"]).strip()
            if "when" in patch and str(patch["when"]).strip():
                r["when"] = str(patch["when"]).strip()
            if "folder" in patch and str(patch["folder"]).strip():
                r["folder"] = sanitize_relative_folder(str(patch["folder"]))
            if "keywords" in patch:
                kws = patch["keywords"]
                if isinstance(kws, str):
                    kws = [x.strip() for x in kws.split(",")]
                r["keywords"] = [str(k).strip().lower() for k in (kws or []) if str(k).strip()]
            if "person" in patch:
                r["person"] = str(patch["person"]).strip() or None
            if "enabled" in patch:
                r["enabled"] = bool(patch["enabled"])
            save_rules(rules)
            invalidate_cache(clear_all=True)
            return r
    raise KeyError(f"Rule not found: {rule_id}")


def delete_rule(rule_id: str) -> bool:
    rules = load_rules()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return False
    save_rules(new_rules)
    invalidate_cache(clear_all=True)
    return True


def resolve_folder_template(folder_tpl: str, doc_text: str = "", pdf_name: str = "") -> str:
    if "{YEAR}" not in folder_tpl:
        return folder_tpl
    years = re.findall(r"\b(20(?:1\d|2\d|3\d))\b", doc_text)
    if not years:
        years = re.findall(r"\b(20(?:1\d|2\d|3\d))\b", pdf_name)
    year = years[0] if years else str(dt.datetime.now().year)
    return folder_tpl.replace("{YEAR}", year)


def match_rules(
    doc_text: str,
    pdf_name: str,
    rules: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rules = rules if rules is not None else load_rules()
    stops = cue_stopwords(profile)
    aliases_map = person_aliases(profile)
    blob = (pdf_name + "\n" + doc_text).lower()
    blob_tokens = set(re.findall(r"[a-z0-9]{2,}", blob))
    matches: list[dict[str, Any]] = []

    for r in rules:
        if not r.get("enabled", True):
            continue
        score = 0
        hit_kws: list[str] = []
        for kw in r.get("keywords") or []:
            kw_l = kw.lower().strip()
            if not kw_l:
                continue
            if " " in kw_l:
                if kw_l in blob:
                    score += 5
                    hit_kws.append(kw_l)
            else:
                if re.search(rf"\b{re.escape(kw_l)}\b", blob):
                    score += 4 if len(kw_l) <= 5 else 3
                    hit_kws.append(kw_l)

        person = (r.get("person") or "").strip().lower()
        person_hit = False
        if person:
            aliases = aliases_map.get(person, (person,))
            if any(re.search(rf"\b{re.escape(a)}\b", blob) for a in aliases):
                person_hit = True
                if hit_kws:
                    score += 3

        when_tokens = {
            t for t in re.findall(r"[a-z0-9]{3,}", (r.get("when") or "").lower())
            if t not in stops
        }
        when_overlap = len(when_tokens & blob_tokens)
        if when_overlap >= 2:
            score += min(when_overlap, 4)

        if hit_kws or score >= 4:
            resolved = resolve_folder_template(r["folder"], doc_text, pdf_name)
            matches.append(
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "when": r.get("when"),
                    "folder": resolved,
                    "folder_template": r.get("folder"),
                    "matched_keywords": hit_kws,
                    "person_match": person_hit,
                    "score": score,
                }
            )

    matches.sort(key=lambda m: (-m["score"], -len(m["matched_keywords"])))
    return matches


def invalidate_cache(
    keywords: set[str] | None = None,
    old_folder: str | None = None,
    clear_all: bool = False,
    profile: dict[str, Any] | None = None,
) -> int:
    with _CACHE_LOCK:
        if not CACHE_FILE.is_file():
            return 0
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                return 0
        except Exception:
            return 0

        if clear_all:
            count = len(cache)
            CACHE_FILE.unlink(missing_ok=True)
            return count

        stops = cue_stopwords(profile)
        kws = {k.lower() for k in (keywords or set()) if len(k) >= 3 and k.lower() not in stops}
        removed = 0
        new_cache: dict[str, Any] = {}
        for k, entry in cache.items():
            drop = False
            if old_folder and entry.get("recommended_folder") == old_folder:
                drop = True
            elif kws:
                hay = " ".join(
                    str(entry.get(f) or "")
                    for f in ("summary", "vendor_or_source", "document_type", "original_filename", "text_preview")
                ).lower()
                hay_tokens = set(re.findall(r"[a-z0-9]{3,}", hay))
                if len(kws & hay_tokens) >= 2:
                    drop = True
            if drop:
                removed += 1
            else:
                new_cache[k] = entry

        if removed > 0:
            tmp = CACHE_FILE.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(json.dumps(new_cache, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, CACHE_FILE)
        return removed


def suggest_rules_from_history() -> list[dict[str, Any]]:
    """Mine user overrides in filing-history.jsonl to propose concrete filing rules."""
    entries = load_history(include_undone=False)
    existing_rules = load_rules()
    existing_folders = {r.get("folder", "").lower() for r in existing_rules if r.get("enabled", True)}

    groups: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if not e.get("user_overrode"):
            continue
        folder = e.get("relative_folder") or ""
        if not folder:
            continue
        tpl = re.sub(r"/(20\d{2})$", "/{YEAR}", folder)
        groups.setdefault(tpl, []).append(e)

    suggestions: list[dict[str, Any]] = []
    for folder_tpl, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if folder_tpl.lower() in existing_folders:
            continue
        vendors = sorted({
            str(i.get("vendor_or_source")).strip()
            for i in items
            if i.get("vendor_or_source") and i.get("vendor_or_source") != "Unknown"
        })
        types = sorted({str(i.get("document_type")).strip() for i in items if i.get("document_type")})
        persons = sorted({
            str(i.get("person")).strip()
            for i in items
            if i.get("person") and i.get("person") not in ("Unknown", "Joint", "Family")
        })
        person = persons[0] if len(persons) == 1 else None

        kw_set: list[str] = []
        seen_kw: set[str] = set()
        for src in vendors + types:
            low = src.lower()
            if low and low not in seen_kw and len(low) >= 3:
                seen_kw.add(low)
                kw_set.append(low)
        for i in items:
            for c in (i.get("cues") or [])[:5]:
                if c not in seen_kw:
                    seen_kw.add(c)
                    kw_set.append(c)

        leaf = folder_tpl.replace("/{YEAR}", "").split("/")[-1]
        when_parts = []
        if types:
            when_parts.append(", ".join(types[:3]))
        if vendors:
            when_parts.append(f"from {', '.join(vendors[:3])}")
        if person:
            when_parts.append(f"for {person}")
        when_str = " ".join(when_parts) or f"Documents matching {leaf}"

        suggestions.append(
            {
                "name": f"{person + ' ' if person and person.lower() not in leaf.lower() else ''}{leaf}",
                "folder": folder_tpl,
                "when": when_str,
                "keywords": kw_set[:8],
                "person": person,
                "override_count": len(items),
                "examples": [i.get("filename") for i in items[:4]],
            }
        )
    return suggestions


def sha256_file(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in _HASH_MEMO:
        return _HASH_MEMO[key]
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if len(_HASH_MEMO) > 500:
        _HASH_MEMO.clear()
    _HASH_MEMO[key] = digest
    return digest


def get_cached_recommendation(pdf_path: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cfg = cfg or get_config()
    try:
        fhash = sha256_file(pdf_path)
    except OSError:
        return None
    cache = load_cache()
    for model_key in (cfg["model_strong"], cfg["model_fast"]):
        rec = cache.get(f"{fhash}:{model_key}")
        if rec:
            return rec
    prefix = f"{fhash}:"
    for k, rec in cache.items():
        if k.startswith(prefix):
            return rec
    return None


def extract_pdf_text(pdf_path: Path, max_pages: int = 4, max_chars: int = 7500) -> str:
    text = ""
    if shutil.which("pdftotext"):
        proc = subprocess.run(
            ["pdftotext", "-l", str(max_pages), str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (proc.stdout or "").strip()

    if len(text) < 40 and shutil.which("tesseract") and shutil.which("magick"):
        with tempfile.TemporaryDirectory(prefix="doxie-filer-ocr-") as tmp:
            img = Path(tmp) / "page1.png"
            conv = subprocess.run(
                ["magick", "-density", "200", f"{pdf_path}[0]", "-background", "white", "-alpha", "remove", str(img)],
                capture_output=True,
                check=False,
            )
            if conv.returncode == 0 and img.is_file():
                ocr = subprocess.run(
                    ["tesseract", str(img), "stdout", "-l", "eng"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                text = (ocr.stdout or "").strip()

    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]..."
    return text


def is_ocr_low_quality(text: str) -> bool:
    """Detect handwritten notes, photos, or garbled OCR where vision mode should be used."""
    stripped = (text or "").strip()
    alnum = sum(c.isalnum() for c in stripped)
    if alnum < 120:
        return True
    weird = sum(1 for c in stripped if not (c.isalnum() or c.isspace() or c in ".,-:/$()&#'\"?!%@+"))
    if weird / max(len(stripped), 1) > 0.04:
        return True
    tokens = stripped.split()
    if len(tokens) < 15:
        return True
    clean_words = 0
    for tok in tokens:
        t = tok.strip(".,-:/()\"'?!;:")
        if len(t) >= 3 and t.isalpha() and any(v in t.lower() for v in "aeiouy"):
            if t.islower() or t.isupper() or (t[0].isupper() and t[1:].islower()):
                clean_words += 1
    return (clean_words / max(len(tokens), 1)) < 0.42


def render_page_image_b64(pdf_path: Path, page: int = 1, dpi: int = 130) -> str | None:
    """Render a PDF page as a compact base64 JPEG for local vision-language models."""
    with tempfile.TemporaryDirectory(prefix="doxie-vlm-") as tmp:
        tmp_dir = Path(tmp)
        if shutil.which("pdftoppm"):
            proc = subprocess.run(
                [
                    "pdftoppm",
                    "-jpeg",
                    "-jpegopt", "quality=80",
                    "-r", str(dpi),
                    "-f", str(page),
                    "-l", str(page),
                    str(pdf_path),
                    str(tmp_dir / "page"),
                ],
                capture_output=True,
                check=False,
            )
            imgs = sorted(tmp_dir.glob("page*.jpg"))
            if proc.returncode == 0 and imgs:
                return base64.b64encode(imgs[0].read_bytes()).decode("ascii")
        if shutil.which("magick"):
            out = tmp_dir / "page.jpg"
            proc = subprocess.run(
                ["magick", "-density", str(dpi), f"{pdf_path}[{page - 1}]", "-background", "white", "-alpha", "remove", "-quality", "80", str(out)],
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0 and out.is_file():
                return base64.b64encode(out.read_bytes()).decode("ascii")
    return None


def list_target_folders(
    target_root: Path,
    pruned: bool = False,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    if not target_root.is_dir():
        return []
    prune_set = set((profile or {}).get("prune_subtrees") or [])
    folders: list[str] = []
    for root, dirs, _files in os.walk(target_root):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and not d.endswith("_files")
            and not d.endswith(".app")
        )
        rel_root = Path(root).relative_to(target_root)
        if str(rel_root) != ".":
            rel_str = rel_root.as_posix()
            if pruned and any(rel_str == p or rel_str.startswith(p + "/") for p in prune_set):
                dirs[:] = []
                continue
            folders.append(rel_str)
    return sorted(folders)


def browse_folder(
    target_root: Path,
    rel_path: str = "",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return immediate child directories, breadcrumbs, and sample files for the folder picker dialog."""
    root_label = target_root.name or "Root"
    target_root = target_root.resolve()
    clean_rel = sanitize_relative_folder(rel_path)
    cur = (target_root / clean_rel).resolve() if clean_rel else target_root
    if cur != target_root and target_root not in cur.parents:
        raise ValueError("Path must be inside target root")
    if not cur.is_dir():
        raise FileNotFoundError(f"Folder not found: {clean_rel or '/'}")

    hidden_dirs = set((profile or {}).get("hide_in_browser") or ["Scans"])
    crumbs = [{"label": root_label, "path": ""}]
    if clean_rel:
        acc: list[str] = []
        for part in clean_rel.split("/"):
            acc.append(part)
            crumbs.append({"label": part, "path": "/".join(acc)})

    child_dirs: list[dict[str, Any]] = []
    files: list[str] = []
    for entry in sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        name = entry.name
        if name.startswith(".") or name.endswith("_files") or name.endswith(".app"):
            continue
        if entry.is_dir():
            rel_child = f"{clean_rel}/{name}" if clean_rel else name
            if rel_child in hidden_dirs:
                continue
            has_children = False
            try:
                for sub in entry.iterdir():
                    if sub.is_dir() and not sub.name.startswith(".") and not sub.name.endswith(".app"):
                        has_children = True
                        break
            except OSError:
                pass
            child_dirs.append({"name": name, "path": rel_child, "hasChildren": has_children})
        elif entry.is_file():
            files.append(name)

    return {
        "rootName": root_label,
        "path": clean_rel,
        "parent": "/".join(clean_rel.split("/")[:-1]) if "/" in clean_rel else ("" if clean_rel else None),
        "breadcrumbs": crumbs,
        "dirs": child_dirs,
        "files": files[:15],
        "totalFiles": len(files),
    }


def sample_folder_filenames(target_root: Path, rel_folder: str, limit: int = 4) -> list[str]:
    folder = target_root / rel_folder
    if not folder.is_dir():
        return []
    names: list[str] = []
    for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and not p.name.startswith("."):
            names.append(p.name)
            if len(names) >= limit:
                break
    return names


def load_history(limit: int | None = None, include_undone: bool = True) -> list[dict[str, Any]]:
    if not HISTORY_FILE.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if include_undone or not obj.get("undone"):
                entries.append(obj)
        except json.JSONDecodeError:
            continue
    entries.reverse()
    if limit is not None:
        return entries[:limit]
    return entries


def append_history(entry: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def rewrite_history(entries_newest_first: list[dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for entry in reversed(entries_newest_first):
            f.write(json.dumps(entry) + "\n")
    os.replace(tmp, HISTORY_FILE)


def relevant_history_examples(
    doc_text: str,
    limit: int = 8,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    entries = load_history(include_undone=False)
    if not entries or not (doc_text or "").strip():
        return []
    stops = cue_stopwords(profile)
    doc_tokens = {t for t in re.findall(r"[a-z0-9]{3,}", doc_text.lower()) if t not in stops}
    if len(doc_tokens) < 3:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for e in entries:
        cues_str = " ".join(e.get("cues") or [])
        blob = " ".join(
            str(e.get(k) or "")
            for k in (
                "summary",
                "vendor_or_source",
                "document_type",
                "person",
                "relative_folder",
                "filename",
                "user_note",
                "text_excerpt",
            )
        ).lower() + " " + cues_str.lower()
        tokens = {t for t in re.findall(r"[a-z0-9]{3,}", blob) if t not in stops}
        raw_overlap = len(doc_tokens & tokens)
        if raw_overlap < 2:
            continue
        score = float(raw_overlap) * (2.0 if e.get("user_overrode") else 1.0)
        scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _score, e in scored[:limit]]


def load_cache() -> dict[str, Any]:
    with _CACHE_LOCK:
        if not CACHE_FILE.is_file():
            return {}
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def update_cache_entry(cache_key: str, entry: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        cache: dict[str, Any] = {}
        if CACHE_FILE.is_file():
            try:
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cache = data
            except Exception:
                cache = {}
        cache[cache_key] = entry
        if len(cache) > 200:
            keys = sorted(cache.keys(), key=lambda k: cache[k].get("cachedAt", ""), reverse=True)[:200]
            cache = {k: cache[k] for k in keys}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, CACHE_FILE)


def select_prompt_folders(
    all_folders: list[str],
    doc_text: str,
    pdf_name: str,
    max_folders: int = 240,
    pinned_folders: set[str] | None = None,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    """Build a compact, high-signal folder list that fits cleanly in local LLM context."""
    prof = profile or {}
    prune_set = set(prof.get("prune_subtrees") or [])
    conditional_deep: dict[str, list[str]] = prof.get("conditional_deep") or {}
    deep_rules: dict[str, dict[str, Any]] = prof.get("deep_include_roots") or {}
    year_pruned = set(prof.get("year_pruned_roots") or [])

    doc_lower = (pdf_name + " " + doc_text).lower()
    doc_tokens = set(re.findall(r"[a-z0-9]{2,}", doc_lower))
    cur_year = dt.datetime.now().year
    recent_years = {str(y) for y in range(cur_year - 3, cur_year + 2)}

    pinned = {sanitize_relative_folder(p) for p in (pinned_folders or set()) if sanitize_relative_folder(p)}
    selected: set[str] = set(pinned)

    for f in all_folders:
        if any(f == p or f.startswith(p + "/") for p in prune_set):
            continue
        parts = f.split("/")
        depth = len(parts)
        top = parts[0]

        if top in year_pruned and depth >= 2:
            ym = re.match(r"^(\d{4})\b", parts[1])
            if ym and ym.group(1) not in recent_years and ym.group(1) not in doc_lower:
                continue

        skip_deep = False
        for prefix, kws in conditional_deep.items():
            if f.startswith(prefix + "/") and not any(str(k).lower() in doc_lower for k in kws):
                skip_deep = True
                break
        if skip_deep:
            continue

        if depth <= 2:
            selected.add(f)
        elif top in deep_rules:
            rule = deep_rules[top]
            max_d = int(rule.get("max_depth", 3))
            d4_parents = set(rule.get("depth4_parents") or [])
            if depth <= 3 and depth <= max_d:
                selected.add(f)
            elif depth == 4 and max_d >= 4 and (not d4_parents or parts[1] in d4_parents):
                selected.add(f)
        elif depth == 3 and not deep_rules:
            selected.add(f)

    scored: list[tuple[int, str]] = []
    for f in all_folders:
        if f in selected or any(f == p or f.startswith(p + "/") for p in prune_set):
            continue
        ftoks = set(re.findall(r"[a-z0-9]{2,}", f.lower()))
        overlap = len(doc_tokens & ftoks)
        if overlap > 0:
            scored.append((overlap, f))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    for _score, f in scored[:45]:
        selected.add(f)

    ordered = sorted(selected)
    if len(ordered) > max_folders:
        def rank(path: str) -> tuple[int, int, int, str]:
            is_pinned = 0 if path in pinned else 1
            ftoks = set(re.findall(r"[a-z0-9]{2,}", path.lower()))
            hit = 0 if (doc_tokens & ftoks) else 1
            return (is_pinned, hit, path.count("/"), path)
        ordered = sorted(ordered, key=rank)[:max_folders]
        ordered.sort()
    return ordered


def lms_running_models() -> list[dict[str, Any]]:
    if not LMS_BIN.is_file():
        return []
    proc = subprocess.run([str(LMS_BIN), "ps", "--json"], capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
        return [m for m in data if isinstance(m, dict) and m.get("type") == "llm"]
    except Exception:
        return []


def ensure_lmstudio_model(cfg: dict[str, Any], model_key: str) -> str:
    lms_url = cfg["lmstudio_url"]
    if LMS_BIN.is_file():
        subprocess.run([str(LMS_BIN), "daemon", "up"], capture_output=True, text=True, check=False)
        try:
            urllib.request.urlopen(f"{lms_url}/v1/models", timeout=2.0)
        except Exception:
            host_port = lms_url.replace("http://", "").replace("https://", "")
            host, _, port = host_port.partition(":")
            subprocess.run(
                [str(LMS_BIN), "server", "start", "--bind", host or "127.0.0.1", "--port", port or "1234"],
                capture_output=True,
                text=True,
                check=False,
            )

        for m in lms_running_models():
            ident = m.get("identifier") or m.get("modelKey") or ""
            mkey = m.get("modelKey") or ""
            if mkey == model_key or ident == model_key or ident.startswith(model_key):
                return ident or model_key

        proc = subprocess.run(
            [
                str(LMS_BIN),
                "load",
                model_key,
                "--context-length",
                str(cfg["context_length"]),
                "--parallel",
                "1",
                "-y",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to load LM Studio model {model_key}: {proc.stderr.strip() or proc.stdout.strip()}")

        for m in lms_running_models():
            ident = m.get("identifier") or m.get("modelKey") or ""
            mkey = m.get("modelKey") or ""
            if mkey == model_key or ident == model_key or ident.startswith(model_key):
                return ident or model_key
        return model_key
    return model_key


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = strip_thinking(text)
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Model did not return a JSON object: {cleaned[:300]}")
    return json.loads(cleaned[start : end + 1])


def sanitize_relative_folder(folder: str) -> str:
    folder = (folder or "").strip().strip("/").replace("\\", "/")
    parts = [p.strip() for p in folder.split("/") if p.strip() and p.strip() not in (".", "..")]
    return "/".join(parts)


def sanitize_filename(name: str, fallback: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def build_prompt(
    pdf_name: str,
    doc_text: str,
    folders: list[str],
    history_examples: list[dict[str, Any]],
    target_root_name: str = "Documents",
    profile: dict[str, Any] | None = None,
    rules: list[dict[str, Any]] | None = None,
    matched_rules: list[dict[str, Any]] | None = None,
    use_vision: bool = False,
) -> tuple[str, str]:
    prof = profile or {}
    household = prof.get("household_description") or "a personal/family document archive"
    notes = prof.get("top_level_notes") or []
    layout_block = (
        f"Top-level layout inside '{target_root_name}':\n" + "\n".join(f"- {n}" for n in notes) + "\n\n"
        if notes
        else ""
    )
    people_names = list((prof.get("people") or {}).keys())
    person_choices = " | ".join(people_names + ["Joint", "Family", "Unknown"]) if people_names else "Person name | Joint | Family | Unknown"

    system_prompt = (
        f"You are a careful, privacy-focused personal document filing assistant for {household}. "
        f"Your job is to inspect a scanned PDF (its OCR text and/or attached page image) and recommend the single best destination folder "
        f"inside '{target_root_name}', explain WHY clearly, provide 2-3 sensible alternative folders, and suggest "
        f"a clean descriptive PDF filename.\n\n"
        f"{layout_block}"
        "Priority Rules:\n"
        "1. NEVER invent vendor names, people, or details from the rules or past examples if they are NOT actually in the document text or image!\n"
        "2. When a USER FILING RULE or USER OVERRIDE CORRECTION genuinely matches the actual document content, follow it first.\n"
        "3. Strongly prefer an EXISTING folder from the provided folder list whenever one fits well. Never choose the inbox ('Scans').\n"
        "4. recommended_folder MUST be a folder path ONLY—NEVER include the .pdf filename inside recommended_folder.\n"
        "5. Only propose a new subfolder (set is_new_folder=true) when no existing folder is specific enough "
        "—and always include the closest existing folder as an alternative.\n"
        "6. Pay close attention to WHO the document is for and WHAT kind of record it is "
        "(handwritten note vs medical record vs healthcare receipt vs school vs financial vs auto vs real estate vs tax).\n"
        "7. For suggested_filename, use a clean, readable format like 'YYYY-MM-DD Vendor Short Description - Person.pdf' "
        "(omit Person if it applies to the whole household/joint; use the document date if present, otherwise the scan date from the input filename).\n"
        "8. If a user filing rule genuinely applies, set matched_rule to that rule's exact name (otherwise null) and mention it in 'why'.\n"
        "9. Output ONLY valid JSON matching the requested schema—no prose outside the JSON."
    )

    folder_block = "\n".join(folders)

    enabled_rules = [r for r in (rules or []) if r.get("enabled", True)]
    rule_lines = []
    for r in enabled_rules:
        resolved = resolve_folder_template(r["folder"], doc_text, pdf_name)
        kws = ", ".join(r.get("keywords") or [])
        person_s = f" | person={r['person']}" if r.get("person") else ""
        kw_s = f" | cues=[{kws}]" if kws else ""
        rule_lines.append(f"- [{r['name']}] When: {r['when']}{person_s}{kw_s} => File to: {resolved}")
    rules_block = "\n".join(rule_lines) if rule_lines else "(No custom filing rules defined yet.)"

    matched_lines = []
    for m in (matched_rules or [])[:4]:
        kws = ", ".join(m.get("matched_keywords") or [])
        matched_lines.append(
            f"- RULE HIT [{m['name']}] -> {m['folder']} (matched cues: {kws or 'description match'})"
        )
    matched_block = "\n".join(matched_lines) if matched_lines else "(None deterministically matched)"

    history_lines = []
    for h in history_examples:
        note_s = f" | User note: {h['user_note']}" if h.get("user_note") else ""
        if h.get("user_overrode"):
            history_lines.append(
                f"- CORRECTION (USER OVERRODE AI): {h.get('summary') or h.get('original_filename')} "
                f"(source={h.get('vendor_or_source')}, type={h.get('document_type')}, person={h.get('person')}) "
                f"| AI wrongly suggested: {h.get('ai_recommended_folder')} "
                f"| User ACTUALLY filed to: {h.get('relative_folder')}/{h.get('filename')}{note_s}"
            )
        else:
            history_lines.append(
                f"- Approved: {h.get('summary') or h.get('original_filename')} "
                f"(source={h.get('vendor_or_source')}, type={h.get('document_type')}, person={h.get('person')}) "
                f"=> Filed to: {h.get('relative_folder')}/{h.get('filename')}{note_s}"
            )
    history_block = "\n".join(history_lines) if history_lines else "(No closely matching past filings for this document.)"

    if use_vision:
        doc_section = (
            "IMPORTANT: The OCR text was sparse or garbled (likely a handwritten note, photo, or visual scan). "
            "Read the ATTACHED IMAGE of page 1 directly to understand what this document is, and ignore any garbled OCR artifacts."
        )
    else:
        doc_section = f'EXTRACTED TEXT FROM PDF:\n"""\n{doc_text or "(No text extracted from PDF)"}\n"""'

    user_prompt = f"""Input filename: {pdf_name}

USER FILING RULES (follow ONLY if the actual document matches):
{rules_block}

RULES THAT MATCHED CUES IN THIS PDF:
{matched_block}

RELEVANT PAST FILING DECISIONS & USER CORRECTIONS:
{history_block}

EXISTING FOLDERS IN "{target_root_name}":
{folder_block}

{doc_section}

Return a single JSON object with these exact keys:
{{
  "summary": "One concise sentence describing what this document ACTUALLY is",
  "document_date": "YYYY-MM-DD or null",
  "person": "{person_choices}",
  "vendor_or_source": "Organization, sender, or 'Handwritten Note' if personal note",
  "document_type": "Short category (e.g. Gift List, Handwritten Note, Medical Record, Receipt, Bank Statement, Tax Form)",
  "recommended_folder": "Exact relative FOLDER path under {target_root_name} (NO .pdf filename, NO leading slash)",
  "is_new_folder": false,
  "suggested_filename": "Clean descriptive filename ending in .pdf",
  "confidence": "high | medium | low",
  "matched_rule": "Exact name of the matching USER FILING RULE, or null",
  "why": "2-3 concise sentences explaining what you actually saw in the document and why this folder fits",
  "alternatives": [
    {{"folder": "Relative/Folder/Path", "is_new_folder": false, "why": "Brief reason"}},
    {{"folder": "Another/Folder/Path", "is_new_folder": true, "why": "Brief reason"}}
  ]
}}"""
    return system_prompt, user_prompt


def heuristic_recommend(
    pdf_path: Path,
    doc_text: str,
    folders: list[str],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Offline keyword fallback if LM Studio cannot be reached."""
    text_l = (pdf_path.name + " " + doc_text).lower()
    tokens = set(re.findall(r"[a-z0-9]{3,}", text_l))
    aliases = person_aliases(profile)
    fallback_folder = (profile or {}).get("fallback_folder") or (folders[0] if folders else "Unsorted")

    scored: list[tuple[int, str]] = []
    for f in folders:
        fl = f.lower()
        ftoks = set(re.findall(r"[a-z0-9]{3,}", fl))
        score = len(tokens & ftoks) * 2
        for canonical, names in aliases.items():
            if any(n in tokens for n in names) and any(n in fl for n in names + (canonical,)):
                score += 4
        scored.append((score, f))

    scored.sort(key=lambda x: (-x[0], len(x[1])))
    top_folder = scored[0][1] if scored and scored[0][0] > 0 else fallback_folder
    alts = [
        {"folder": f, "is_new_folder": False, "why": "Keyword overlap with folder path"}
        for s, f in scored[1:4]
        if f != top_folder and s > 0
    ]
    return {
        "summary": f"Scanned PDF ({pdf_path.name})",
        "document_date": None,
        "person": "Unknown",
        "vendor_or_source": "Unknown",
        "document_type": "Scanned Document",
        "recommended_folder": top_folder,
        "is_new_folder": False,
        "suggested_filename": pdf_path.name,
        "confidence": "low",
        "matched_rule": None,
        "why": "Fallback keyword match (LM Studio was unavailable). Review folder before approving.",
        "alternatives": alts,
        "model": "heuristic-fallback",
        "fallback": True,
    }


RECOMMENDATION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "filing_recommendation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "document_date": {"type": ["string", "null"]},
                "person": {"type": "string"},
                "vendor_or_source": {"type": "string"},
                "document_type": {"type": "string"},
                "recommended_folder": {"type": "string"},
                "is_new_folder": {"type": "boolean"},
                "suggested_filename": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "matched_rule": {"type": ["string", "null"]},
                "why": {"type": "string"},
                "alternatives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "folder": {"type": "string"},
                            "is_new_folder": {"type": "boolean"},
                            "why": {"type": "string"},
                        },
                        "required": ["folder", "is_new_folder", "why"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "summary",
                "document_date",
                "person",
                "vendor_or_source",
                "document_type",
                "recommended_folder",
                "is_new_folder",
                "suggested_filename",
                "confidence",
                "matched_rule",
                "why",
                "alternatives",
            ],
            "additionalProperties": False,
        },
    },
}


def recommend_for_pdf(
    pdf_path: Path,
    cfg: dict[str, Any] | None = None,
    tier: str = "fast",
    model_override: str | None = None,
    bypass_cache: bool = False,
) -> dict[str, Any]:
    cfg = cfg or get_config()
    profile = cfg.get("profile") or load_profile()
    pdf_path = pdf_path.expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    model_key = model_override or (cfg["model_strong"] if tier == "strong" else cfg["model_fast"])
    file_hash = sha256_file(pdf_path)
    cache_key = f"{file_hash}:{model_key}"

    if not bypass_cache:
        cache = load_cache()
        if cache_key in cache:
            rec = dict(cache[cache_key])
            rec["cached"] = True
            return rec

    doc_text = extract_pdf_text(pdf_path, max_pages=3, max_chars=4200)
    use_vision = is_ocr_low_quality(doc_text)
    page_b64 = render_page_image_b64(pdf_path, page=1, dpi=130) if use_vision else None
    if page_b64 is None:
        use_vision = False

    target_root: Path = cfg["target_root"]
    target_root_name = target_root.name or "Documents"
    all_folders = list_target_folders(target_root, pruned=False, profile=profile)
    all_folders_set = set(all_folders)
    rules = load_rules()
    matched_rules = [] if use_vision else match_rules(doc_text, pdf_path.name, rules, profile=profile)
    history = [] if use_vision else relevant_history_examples(doc_text, profile=profile)

    pinned_folders: set[str] = set()
    for h in load_history(include_undone=False):
        if h.get("relative_folder"):
            pinned_folders.add(h["relative_folder"])
    for r in rules:
        if r.get("enabled", True) and r.get("folder"):
            pinned_folders.add(resolve_folder_template(r["folder"], doc_text, pdf_path.name))

    prompt_folders = select_prompt_folders(
        all_folders,
        "" if use_vision else doc_text,
        pdf_path.name,
        pinned_folders=pinned_folders,
        profile=profile,
    )

    try:
        with _LLM_LOCK:
            if not bypass_cache:
                cache = load_cache()
                if cache_key in cache:
                    rec = dict(cache[cache_key])
                    rec["cached"] = True
                    return rec

            loaded_model = ensure_lmstudio_model(cfg, model_key)
            sys_prompt, usr_prompt = build_prompt(
                pdf_path.name,
                doc_text,
                prompt_folders,
                history,
                target_root_name=target_root_name,
                profile=profile,
                rules=rules,
                matched_rules=matched_rules,
                use_vision=use_vision,
            )
            user_content: Any = (
                [
                    {"type": "text", "text": usr_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{page_b64}"}},
                ]
                if (use_vision and page_b64)
                else usr_prompt
            )
            payload = {
                "model": loaded_model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content},
                ],
                "response_format": RECOMMENDATION_RESPONSE_FORMAT,
                "reasoning_effort": "none" if tier == "fast" else "low",
                "temperature": 0.15,
                "max_tokens": 1200 if tier == "fast" else 3072,
            }
            req = urllib.request.Request(
                f"{cfg['lmstudio_url']}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=180.0) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as he:
                body_err = he.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"HTTP {he.code}: {body_err[:300]}") from None
        msg = raw["choices"][0]["message"]
        content = msg.get("content") or ""
        parsed = extract_json_object(content)
        parsed["model"] = loaded_model
        parsed["fallback"] = False
        parsed["used_vision"] = bool(use_vision and page_b64)
    except Exception as exc:
        parsed = heuristic_recommend(pdf_path, doc_text, prompt_folders, profile=profile)
        parsed["llm_error"] = str(exc)
        parsed["used_vision"] = False

    fallback_folder = profile.get("fallback_folder") or (all_folders[0] if all_folders else "Unsorted")
    rec_folder = sanitize_relative_folder(parsed.get("recommended_folder", "")) or fallback_folder
    if rec_folder.lower().endswith(".pdf") and "/" in rec_folder:
        rec_folder = rec_folder.rsplit("/", 1)[0]
    if rec_folder in set(profile.get("hide_in_browser") or ["Scans"]):
        rec_folder = fallback_folder
    parsed["recommended_folder"] = rec_folder
    parsed["is_new_folder"] = rec_folder not in all_folders_set
    parsed["suggested_filename"] = sanitize_filename(parsed.get("suggested_filename", ""), pdf_path.name)
    parsed["confidence"] = str(parsed.get("confidence", "medium")).lower()
    if parsed["confidence"] not in ("high", "medium", "low"):
        parsed["confidence"] = "medium"

    clean_alts: list[dict[str, Any]] = []
    seen_folders = {rec_folder}
    for alt in parsed.get("alternatives") or []:
        if not isinstance(alt, dict):
            continue
        af = sanitize_relative_folder(alt.get("folder", ""))
        if not af or af in seen_folders:
            continue
        seen_folders.add(af)
        clean_alts.append(
            {
                "folder": af,
                "is_new_folder": af not in all_folders_set,
                "why": str(alt.get("why", "")).strip(),
            }
        )
    parsed["alternatives"] = clean_alts[:4]
    parsed["sibling_examples"] = sample_folder_filenames(target_root, rec_folder, limit=4)
    parsed["source_path"] = str(pdf_path)
    parsed["original_filename"] = pdf_path.name
    parsed["page_count"] = get_pdf_page_count(pdf_path)
    parsed["text_preview"] = doc_text[:600]
    parsed["cues"] = extract_doc_cues(doc_text, max_terms=18, profile=profile)
    parsed["matched_rules"] = matched_rules[:3]
    if not parsed.get("matched_rule") and matched_rules:
        for m in matched_rules:
            if m["folder"] == rec_folder:
                parsed["matched_rule"] = m["name"]
                break
    parsed["cachedAt"] = dt.datetime.now().astimezone().isoformat()
    parsed["cached"] = False

    if not parsed.get("fallback"):
        update_cache_entry(cache_key, parsed)

    return parsed


def approve_move(
    source_pdf: Path,
    relative_folder: str,
    filename: str,
    cfg: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
    copy_only: bool = False,
    user_note: str | None = None,
    save_rule: bool = False,
    rule_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or get_config()
    profile = cfg.get("profile") or load_profile()
    target_root: Path = cfg["target_root"].resolve()
    source_pdf = source_pdf.expanduser().resolve()
    if not source_pdf.is_file():
        raise FileNotFoundError(f"Source PDF not found: {source_pdf}")

    rel_folder = sanitize_relative_folder(relative_folder)
    if not rel_folder:
        raise ValueError("Destination folder cannot be empty")
    dest_dir = (target_root / rel_folder).resolve()
    if target_root not in dest_dir.parents and dest_dir != target_root:
        raise ValueError(f"Destination folder must be inside {cfg['target_root'].name}")

    rec = recommendation or {}
    cues = rec.get("cues") or []
    text_excerpt = rec.get("text_preview") or ""
    if not cues:
        doc_text = extract_pdf_text(source_pdf, max_pages=2, max_chars=2000)
        cues = extract_doc_cues(doc_text, max_terms=18, profile=profile)
        if not text_excerpt:
            text_excerpt = doc_text[:500]

    clean_name = sanitize_filename(filename, source_pdf.name)
    created_folder = not dest_dir.exists()
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / clean_name
    if dest_path.exists():
        stem = dest_path.stem
        for i in range(2, 1000):
            cand = dest_dir / f"{stem} ({i}).pdf"
            if not cand.exists():
                dest_path = cand
                clean_name = cand.name
                break

    if copy_only:
        staged = dest_dir / f".{clean_name}.tmp.{os.getpid()}"
        shutil.copy2(source_pdf, staged)
        os.replace(staged, dest_path)
    else:
        try:
            os.rename(source_pdf, dest_path)
        except OSError:
            staged = dest_dir / f".{clean_name}.tmp.{os.getpid()}"
            shutil.copy2(source_pdf, staged)
            os.replace(staged, dest_path)
            source_pdf.unlink()

    user_overrode = bool(rec.get("recommended_folder") and rec.get("recommended_folder") != rel_folder)
    clean_note = (user_note or "").strip() or None
    entry = {
        "id": dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(str(dest_path).encode()).hexdigest()[:6],
        "timestamp": dt.datetime.now().astimezone().isoformat(),
        "action": "copy" if copy_only else "move",
        "source_path": str(source_pdf),
        "dest_path": str(dest_path),
        "relative_folder": rel_folder,
        "filename": clean_name,
        "original_filename": source_pdf.name,
        "created_folder": created_folder,
        "summary": rec.get("summary"),
        "document_type": rec.get("document_type"),
        "vendor_or_source": rec.get("vendor_or_source"),
        "person": rec.get("person"),
        "cues": cues[:18],
        "text_excerpt": text_excerpt[:400],
        "user_note": clean_note,
        "ai_recommended_folder": rec.get("recommended_folder"),
        "user_overrode": user_overrode,
        "model": rec.get("model"),
        "undone": False,
    }
    append_history(entry)

    created_rule = None
    invalidated_count = 0
    if save_rule:
        rd = rule_data or {}
        rule_folder = rd.get("folder") or re.sub(r"/(20\d{2})$", "/{YEAR}", rel_folder)
        raw_kws = rd.get("keywords")
        if isinstance(raw_kws, str):
            kws = [x.strip() for x in raw_kws.split(",") if x.strip()]
        elif isinstance(raw_kws, list):
            kws = [str(x).strip() for x in raw_kws if str(x).strip()]
        else:
            kws = []
            for v in (rec.get("vendor_or_source"), rec.get("document_type")):
                if v and str(v).lower() not in ("unknown", "receipt", "scanned document"):
                    kws.append(str(v).lower())
            kws.extend(cues[:5])
        leaf = rel_folder.split("/")[-1]
        person_val = rec.get("person")
        r_name = (rd.get("name") or "").strip() or (
            f"{person_val} {leaf}"
            if person_val and person_val not in ("Unknown", "Joint", "Family") and person_val.lower() not in leaf.lower()
            else leaf
        )
        r_when = (rd.get("when") or "").strip() or clean_note or (
            f"{rec.get('document_type') or 'Documents'} from {rec.get('vendor_or_source') or leaf}"
        )
        r_person = rd.get("person") if "person" in rd else (
            person_val if person_val not in ("Unknown", "Joint", "Family", None) else None
        )
        created_rule = add_rule(
            name=r_name,
            folder=rule_folder,
            when=r_when,
            keywords=kws,
            person=r_person,
            source="override",
        )
        invalidated_count = -1
    elif user_overrode:
        kw_pool = set(cues[:12])
        for fld in (rec.get("vendor_or_source"), rec.get("document_type"), clean_note):
            if fld:
                kw_pool.update(re.findall(r"[a-z0-9]{3,}", str(fld).lower()))
        invalidated_count = invalidate_cache(
            keywords=kw_pool,
            old_folder=rec.get("recommended_folder"),
            clear_all=False,
            profile=profile,
        )

    entry["created_rule"] = created_rule
    entry["invalidated_cache_count"] = invalidated_count
    return entry


def undo_move(entry_id: str | None = None) -> dict[str, Any]:
    entries = load_history(include_undone=True)
    target_entry = None
    for e in entries:
        if e.get("undone"):
            continue
        if entry_id is None or e.get("id") == entry_id:
            target_entry = e
            break
    if not target_entry:
        raise RuntimeError("No undoable move found in filing history.")

    dest_path = Path(target_entry["dest_path"])
    src_path = Path(target_entry["source_path"])
    if not dest_path.is_file():
        raise FileNotFoundError(f"Filed PDF no longer exists at {dest_path}")

    src_path.parent.mkdir(parents=True, exist_ok=True)
    restore_path = src_path
    if restore_path.exists():
        stem = restore_path.stem
        for i in range(2, 1000):
            cand = restore_path.parent / f"{stem}-restored-{i}.pdf"
            if not cand.exists():
                restore_path = cand
                break

    if target_entry.get("action") == "copy":
        dest_path.unlink()
    else:
        try:
            os.rename(dest_path, restore_path)
        except OSError:
            shutil.copy2(dest_path, restore_path)
            dest_path.unlink()

    target_entry["undone"] = True
    target_entry["undone_at"] = dt.datetime.now().astimezone().isoformat()
    target_entry["restored_path"] = str(restore_path)
    rewrite_history(entries)
    return target_entry
