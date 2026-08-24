#!/usr/bin/env python3
"""Read-only Zotero to Obsidian synchronization helper.

The script performs deterministic discovery, caching, validation, and Markdown
rendering. It deliberately does not call a language model; the Codex skill
creates the summary JSON between `scan` and `render`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "http://localhost:23119/api"
SCHEMA_VERSION = 1
AUTO_END = "<!-- codex:auto:end -->"
COLLECTIONS_START = "<!-- codex:collections:start -->"
COLLECTIONS_END = "<!-- codex:collections:end -->"
WATCH_START = "<!-- codex:watch-status:start -->"
WATCH_END = "<!-- codex:watch-status:end -->"
IGNORED_ITEM_TYPES = {"attachment", "note", "annotation"}
SUMMARY_FIELDS = {
    "summary_basis",
    "keywords",
    "brief_summary",
    "scientific_question",
    "datasets",
    "methods",
    "main_findings",
    "scientific_problem_solved",
    "limitations",
    "evidence_notes",
}


class SyncError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Avoid replacing an identical file. On Windows, Obsidian or its indexer can
    # briefly hold an existing note open and make an otherwise unnecessary
    # os.replace fail with PermissionError.
    if path.exists():
        try:
            if path.read_text(encoding="utf-8-sig") == text:
                return
        except (OSError, UnicodeError):
            pass
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def vault_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def paths(vault: Path) -> dict[str, Path]:
    root = vault / "Zotero"
    sync = root / ".sync"
    return {
        "root": root,
        "papers": root / "Papers",
        "archive": root / "Archive",
        "reviews": root / "Reviews",
        "collections": root / "Collections",
        "sync": sync,
        "pending": sync / "pending",
        "processed": sync / "processed",
        "state": sync / "state.json",
        "config": sync / "config.json",
        "collection_manifest": sync / "generated_collection_views.json",
        "base": root / "Library.base",
        "dashboard": root / "Zotero Dashboard.md",
    }


def ensure_initialized(vault: Path) -> dict[str, Path]:
    resolved = paths(vault)
    if not resolved["config"].exists():
        raise SyncError(
            f"Vault is not initialized: {resolved['config']}. Run init-vault first."
        )
    return resolved


def load_config(vault: Path) -> dict[str, Any]:
    resolved = ensure_initialized(vault)
    config = read_json(resolved["config"], {})
    required = {"library_kind", "library_id", "max_fulltext_chars", "ignore_tags"}
    missing = sorted(required - set(config))
    if missing:
        raise SyncError(f"Config is missing fields: {', '.join(missing)}")
    config.setdefault("watched_collections", [])
    return config


def save_config(vault: Path, config: dict[str, Any]) -> None:
    atomic_write_json(paths(vault)["config"], config)


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "server_id": None,
        "library_version": None,
        "initialized_at": now_iso(),
        "last_scan_at": None,
        "items": {},
        "watch_status": {},
    }


def load_state(vault: Path) -> dict[str, Any]:
    resolved = paths(vault)
    state = read_json(resolved["state"], default_state())
    if state.get("schema_version") != SCHEMA_VERSION:
        raise SyncError("Unsupported state schema. Back up .sync and reinitialize.")
    state.setdefault("items", {})
    state.setdefault("watch_status", {})
    return state


def save_state(vault: Path, state: dict[str, Any]) -> None:
    atomic_write_json(paths(vault)["state"], state)


def header_value(headers: Any, name: str) -> str | None:
    return headers.get(name) or headers.get(name.lower())


def api_get(path: str, params: dict[str, Any] | None = None) -> tuple[Any, Any]:
    url = f"{API_ROOT}/{path.lstrip('/')}"
    if params:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{url}?{query}"
    request = Request(url, headers={"Zotero-API-Version": "3"})
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            if raw and ("json" in content_type or raw[:1] in (b"{", b"[")):
                data = json.loads(raw.decode("utf-8"))
            else:
                data = raw.decode("utf-8", errors="replace")
            return data, response.headers
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SyncError(f"Zotero API HTTP {exc.code} for {url}: {body[:300]}") from exc
    except URLError as exc:
        raise SyncError(
            "Cannot connect to Zotero local API. Open Zotero and enable Settings → "
            "Advanced → Allow other applications on this computer to communicate with Zotero."
        ) from exc


def api_get_optional(path: str) -> tuple[Any | None, Any | None]:
    try:
        return api_get(path)
    except SyncError as exc:
        if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
            return None, None
        raise


def api_get_all(
    path: str, params: dict[str, Any] | None = None, page_size: int = 100
) -> tuple[list[Any], Any]:
    """Read all Zotero API pages while preserving the final response headers."""
    output: list[Any] = []
    start = 0
    headers: Any = {}
    while True:
        page_params = dict(params or {})
        page_params.update({"start": start, "limit": page_size})
        rows, headers = api_get(path, page_params)
        if not isinstance(rows, list):
            raise SyncError(f"Expected a Zotero list response for {path}")
        output.extend(rows)
        if len(rows) < page_size:
            break
        start += len(rows)
    return output, headers


def server_info() -> dict[str, Any]:
    data, headers = api_get("")
    return {
        "reachable": True,
        "api_version": header_value(headers, "Zotero-API-Version"),
        "server_id": header_value(headers, "Zotero-Server-ID"),
        "response": data,
    }


def library_prefix(config: dict[str, Any]) -> str:
    kind = config["library_kind"]
    if kind not in {"users", "groups"}:
        raise SyncError("library_kind must be users or groups")
    return f"{kind}/{config['library_id']}"


def current_library_version(config: dict[str, Any]) -> tuple[int, str | None]:
    _, headers = api_get(
        f"{library_prefix(config)}/items/top",
        {"format": "versions", "limit": 1},
    )
    raw = header_value(headers, "Last-Modified-Version")
    return int(raw or 0), header_value(headers, "Zotero-Server-ID")


def check_server(state: dict[str, Any], observed: str | None) -> None:
    saved = state.get("server_id")
    if saved and observed and saved != observed:
        raise SyncError(
            f"Zotero server ID changed from {saved} to {observed}. "
            "This is a different database; run bootstrap --reset only after confirming."
        )


def collection_catalog(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows, _ = api_get_all(f"{library_prefix(config)}/collections", {"format": "json"})
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        data = row.get("data", row)
        key = data.get("key") or row.get("key")
        if key:
            by_key[key] = data

    path_cache: dict[str, str] = {}
    root_cache: dict[str, str] = {}

    def resolve_path(key: str, trail: set[str] | None = None) -> str:
        if key in path_cache:
            return path_cache[key]
        trail = set(trail or ())
        if key in trail:
            return by_key.get(key, {}).get("name", key)
        trail.add(key)
        data = by_key.get(key, {})
        name = data.get("name", key)
        parent = data.get("parentCollection")
        value = f"{resolve_path(parent, trail)}/{name}" if parent else name
        path_cache[key] = value
        return value

    def resolve_root(key: str, trail: set[str] | None = None) -> str:
        if key in root_cache:
            return root_cache[key]
        trail = set(trail or ())
        if key in trail:
            return key
        trail.add(key)
        parent = by_key.get(key, {}).get("parentCollection")
        value = resolve_root(parent, trail) if parent else key
        root_cache[key] = value
        return value

    for key in by_key:
        resolve_path(key)
        resolve_root(key)
    return {
        key: {
            "key": key,
            "name": data.get("name", key),
            "parent_key": data.get("parentCollection") or None,
            "path": path_cache[key],
            "root_key": root_cache[key],
        }
        for key, data in by_key.items()
    }


def collection_paths(config: dict[str, Any]) -> dict[str, str]:
    return {key: row["path"] for key, row in collection_catalog(config).items()}


def top_level_collections(catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (row for row in catalog.values() if not row.get("parent_key")),
        key=lambda row: (row["name"].casefold(), row["key"]),
    )


def resolve_top_collection(
    catalog: dict[str, dict[str, Any]], selector: str
) -> dict[str, Any]:
    exact_key = catalog.get(selector)
    if exact_key and not exact_key.get("parent_key"):
        return exact_key
    matches = [
        row
        for row in top_level_collections(catalog)
        if row["name"].casefold() == selector.casefold()
    ]
    if not matches:
        raise SyncError(f"No top-level Zotero collection matches: {selector}")
    if len(matches) > 1:
        keys = ", ".join(row["key"] for row in matches)
        raise SyncError(f"Top-level collection name is ambiguous: {selector}. Keys: {keys}")
    return matches[0]


def watched_entry(config: dict[str, Any], selector: str | None) -> dict[str, Any]:
    watched = config.get("watched_collections") or []
    if selector is None:
        if len(watched) == 1:
            return watched[0]
        raise SyncError("Specify --collection when zero or multiple collections are watched.")
    matches = [
        row
        for row in watched
        if row.get("collection_key") == selector
        or str(row.get("name", "")).casefold() == selector.casefold()
    ]
    if not matches:
        raise SyncError(f"Collection is not watched: {selector}")
    if len(matches) > 1:
        raise SyncError(f"Watched collection name is ambiguous; use its collection key: {selector}")
    return matches[0]


def normalized_authors(data: dict[str, Any]) -> list[str]:
    creators = data.get("creators") or []
    chosen = [row for row in creators if row.get("creatorType") == "author"]
    if not chosen:
        chosen = [row for row in creators if row.get("creatorType") == "editor"]
    output: list[str] = []
    for row in chosen:
        name = row.get("name") or " ".join(
            part for part in (row.get("firstName", ""), row.get("lastName", "")) if part
        )
        if name.strip():
            output.append(name.strip())
    return output


def item_tags(data: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for row in data.get("tags") or []:
        value = row.get("tag") if isinstance(row, dict) else str(row)
        if value:
            output.append(value)
    return output


def make_packet(
    item: dict[str, Any], config: dict[str, Any], collections: dict[str, str]
) -> dict[str, Any] | None:
    data = item.get("data", item)
    key = data.get("key") or item.get("key")
    item_type = data.get("itemType")
    if not key or item_type in IGNORED_ITEM_TYPES:
        return None
    tags = item_tags(data)
    ignored = {str(value).casefold() for value in config.get("ignore_tags", [])}
    if ignored.intersection(value.casefold() for value in tags):
        return None

    children, _ = api_get(f"{library_prefix(config)}/items/{key}/children", {"format": "json"})
    attachments: list[dict[str, Any]] = []
    fulltexts: list[str] = []
    indexed_pages = 0
    total_pages = 0
    for child in children or []:
        child_data = child.get("data", child)
        if child_data.get("itemType") != "attachment":
            continue
        attachment_key = child_data.get("key") or child.get("key")
        content_type = child_data.get("contentType", "")
        attachment = {
            "key": attachment_key,
            "title": child_data.get("title", ""),
            "filename": child_data.get("filename", ""),
            "content_type": content_type,
            "link_mode": child_data.get("linkMode", ""),
        }
        if attachment_key and content_type == "application/pdf":
            fulltext_data, _ = api_get_optional(
                f"{library_prefix(config)}/items/{attachment_key}/fulltext"
            )
            if isinstance(fulltext_data, dict) and fulltext_data.get("content"):
                fulltexts.append(fulltext_data["content"])
                indexed_pages += int(fulltext_data.get("indexedPages") or 0)
                total_pages += int(fulltext_data.get("totalPages") or 0)
                attachment["fulltext_available"] = True
            else:
                attachment["fulltext_available"] = False
        attachments.append(attachment)

    combined = "\n\n===== NEXT PDF ATTACHMENT =====\n\n".join(fulltexts)
    limit = int(config.get("max_fulltext_chars") or 300000)
    truncated = len(combined) > limit
    combined = combined[:limit]
    abstract = data.get("abstractNote", "") or ""
    basis = "fulltext" if combined else ("abstract" if abstract else "metadata")
    mapped_collections = [
        collections.get(value, value) for value in (data.get("collections") or [])
    ]

    return {
        "packet_schema": 1,
        "created_at": now_iso(),
        "zotero_item_key": key,
        "source_version": int(data.get("version") or item.get("version") or 0),
        "available_summary_basis": basis,
        "metadata": {
            "item_type": item_type,
            "title": data.get("title", "") or "Untitled",
            "publication_date": data.get("date", "") or "",
            "authors": normalized_authors(data),
            "keywords": tags,
            "abstract": abstract,
            "publication_title": data.get("publicationTitle", "") or "",
            "doi": data.get("DOI", "") or "",
            "url": data.get("url", "") or "",
            "language": data.get("language", "") or "",
            "date_added": data.get("dateAdded", "") or "",
            "date_modified": data.get("dateModified", "") or "",
            "collections": mapped_collections,
        },
        "attachments": attachments,
        "fulltext": {
            "status": "available" if combined else (
                "waiting_for_index" if any(
                    row.get("content_type") == "application/pdf" for row in attachments
                ) else "no_pdf"
            ),
            "indexed_pages": indexed_pages,
            "total_pages": total_pages,
            "truncated": truncated,
            "content": combined,
        },
    }


def fetch_packet(vault: Path, item_key: str) -> Path:
    config = load_config(vault)
    item, _ = api_get(f"{library_prefix(config)}/items/{item_key}", {"format": "json"})
    packet = make_packet(item, config, collection_paths(config))
    if not packet:
        raise SyncError(f"Item {item_key} is not a summarizable regular Zotero item.")
    output = paths(vault)["pending"] / f"{item_key}.json"
    atomic_write_json(output, packet)
    return output


def item_key(item: dict[str, Any]) -> str | None:
    data = item.get("data", item)
    return data.get("key") or item.get("key")


def item_is_summarizable(item: dict[str, Any], config: dict[str, Any]) -> bool:
    data = item.get("data", item)
    if data.get("itemType") in IGNORED_ITEM_TYPES or not item_key(item):
        return False
    ignored = {str(value).casefold() for value in config.get("ignore_tags", [])}
    return not ignored.intersection(value.casefold() for value in item_tags(data))


def mapped_item_collections(
    item: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> list[str]:
    data = item.get("data", item)
    return [
        catalog.get(key, {}).get("path", key) for key in (data.get("collections") or [])
    ]


def collection_inventory(
    config: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    entry: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    root_key = entry["collection_key"]
    include_descendants = bool(entry.get("include_descendants", True))
    keys = [
        key
        for key, row in catalog.items()
        if key == root_key or (include_descendants and row.get("root_key") == root_key)
    ]
    output: dict[str, dict[str, Any]] = {}
    for collection_key in sorted(keys):
        rows, _ = api_get_all(
            f"{library_prefix(config)}/collections/{collection_key}/items/top",
            {"format": "json", "sort": "dateAdded", "direction": "asc"},
        )
        for item in rows:
            if not item_is_summarizable(item, config):
                continue
            key = item_key(item)
            if key is None:
                continue
            previous = output.get(key)
            current_version = int(item.get("version") or item.get("data", {}).get("version") or 0)
            previous_version = int(
                (previous or {}).get("version")
                or (previous or {}).get("data", {}).get("version")
                or -1
            )
            if previous is None or current_version >= previous_version:
                output[key] = item
    return output


def needs_sync(
    item: dict[str, Any],
    state_item: dict[str, Any] | None,
    catalog: dict[str, dict[str, Any]],
) -> bool:
    if not state_item or state_item.get("sync_status", "active") != "active":
        return True
    version = int(item.get("version") or item.get("data", {}).get("version") or 0)
    if version > int(state_item.get("source_version") or 0):
        return True
    return mapped_item_collections(item, catalog) != (state_item.get("collections") or [])


def watched_roots_for_item(
    item: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> list[str]:
    watched_keys = {
        row.get("collection_key") for row in (config.get("watched_collections") or [])
    }
    data = item.get("data", item)
    output: list[str] = []
    for collection_key in data.get("collections") or []:
        root_key = catalog.get(collection_key, {}).get("root_key")
        if root_key in watched_keys and root_key not in output:
            output.append(root_key)
    return output


def safe_filename(value: str, limit: int = 110) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:limit].rstrip(" .") or "Untitled")


def publication_year(value: str) -> int | None:
    match = re.search(r"(?<!\d)(1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)", value or "")
    return int(match.group(1)) if match else None


def collection_parts(collection: str) -> list[str]:
    return [part.strip() for part in collection.split("/") if part.strip()]


def collection_tags(collections: list[str]) -> list[str]:
    """Flatten collection paths into ordered Obsidian-style labels."""
    output: list[str] = []
    for collection in collections:
        for part in collection_parts(collection):
            value = f"#{part}"
            if value not in output:
                output.append(value)
    return output


def collection_prefixes(collections: list[str]) -> set[tuple[str, ...]]:
    """Return every hierarchy node represented by one item's collection paths."""
    output: set[tuple[str, ...]] = set()
    for collection in collections:
        parts = collection_parts(collection)
        for index in range(1, len(parts) + 1):
            output.add(tuple(parts[:index]))
    return output


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return json.dumps(text, ensure_ascii=False)


def yaml_property(lines: list[str], name: str, value: Any) -> None:
    if isinstance(value, list):
        if not value:
            lines.append(f"{name}: []")
        else:
            lines.append(f"{name}:")
            lines.extend(f"  - {yaml_scalar(item)}" for item in value)
    else:
        lines.append(f"{name}: {yaml_scalar(value)}")


def validate_summary(summary: dict[str, Any], packet: dict[str, Any]) -> None:
    missing = SUMMARY_FIELDS - set(summary)
    extra = set(summary) - SUMMARY_FIELDS
    if missing or extra:
        raise SyncError(
            f"Summary schema mismatch. Missing={sorted(missing)}, extra={sorted(extra)}"
        )
    if summary["summary_basis"] not in {"fulltext", "abstract", "metadata"}:
        raise SyncError("summary_basis must be fulltext, abstract, or metadata")
    available = packet.get("available_summary_basis", "metadata")
    rank = {"metadata": 0, "abstract": 1, "fulltext": 2}
    if rank[summary["summary_basis"]] > rank[available]:
        raise SyncError(
            f"Summary claims {summary['summary_basis']} evidence but packet only has {available}."
        )
    for field in ("keywords", "datasets", "methods", "evidence_notes"):
        if not isinstance(summary[field], list) or not all(
            isinstance(value, str) for value in summary[field]
        ):
            raise SyncError(f"{field} must be a list of strings")
    for field in SUMMARY_FIELDS - {"keywords", "datasets", "methods", "evidence_notes"}:
        if not isinstance(summary[field], str):
            raise SyncError(f"{field} must be a string")


def note_path_for(vault: Path, packet: dict[str, Any], state: dict[str, Any]) -> Path:
    key = packet["zotero_item_key"]
    existing = state["items"].get(key, {}).get("note_path")
    if existing:
        return vault / Path(existing)
    metadata = packet["metadata"]
    authors = metadata.get("authors") or ["Unknown"]
    author = authors[0].split()[-1]
    year = publication_year(metadata.get("publication_date", "")) or "n.d."
    title = safe_filename(metadata.get("title", ""), 70)
    filename = safe_filename(f"{author}{year} - {title} [{key}]") + ".md"
    return paths(vault)["papers"] / filename


def md_list(values: list[str]) -> str:
    if not values:
        return "- 文中未明确说明"
    return "\n".join(f"- {value}" for value in values)


def build_note(packet: dict[str, Any], summary: dict[str, Any], existing: str = "") -> str:
    metadata = packet["metadata"]
    key = packet["zotero_item_key"]
    attachments = packet.get("attachments") or []
    pdf = next((row for row in attachments if row.get("content_type") == "application/pdf"), None)
    frontmatter = ["---"]
    values = {
        "tags": ["literature-note", "zotero"],
        "zotero_item_key": key,
        "title": metadata.get("title", ""),
        "publication_date": metadata.get("publication_date", ""),
        "publication_year": publication_year(metadata.get("publication_date", "")),
        "authors": metadata.get("authors") or [],
        "keywords": list(dict.fromkeys((metadata.get("keywords") or []) + summary["keywords"])),
        "zotero_collections": collection_tags(metadata.get("collections") or []),
        "journal": metadata.get("publication_title", ""),
        "doi": metadata.get("doi", ""),
        "url": metadata.get("url", ""),
        "zotero_link": f"zotero://select/library/items/{key}",
        "zotero_pdf_link": (
            f"zotero://open-pdf/library/items/{pdf['key']}" if pdf and pdf.get("key") else ""
        ),
        "summary_basis": summary["summary_basis"],
        "summary_status": "completed",
        "sync_status": "active",
        "brief_summary": summary["brief_summary"],
        "scientific_question": summary["scientific_question"],
        "datasets": summary["datasets"],
        "methods": summary["methods"],
        "main_findings": summary["main_findings"],
        "scientific_problem_solved": summary["scientific_problem_solved"],
        "source_version": packet.get("source_version", 0),
        "last_synced": now_iso(),
    }
    for name, value in values.items():
        yaml_property(frontmatter, name, value)
    frontmatter.append("---")

    evidence = summary.get("evidence_notes") or ["未提供可定位的章节或页码信息"]
    automatic = f"""<!-- codex:auto:start -->
# {metadata.get('title', 'Untitled')}

> [!info] 同步状态
> 总结依据：`{summary['summary_basis']}` · Zotero itemKey：`{key}` · 全文状态：`{packet.get('fulltext', {}).get('status', 'unknown')}`

## 简要总结

{summary['brief_summary']}

## 作者提出的科学问题

{summary['scientific_question']}

## Dataset / 研究对象

{md_list(summary['datasets'])}

## Methods

{md_list(summary['methods'])}

## 主要结论

{summary['main_findings']}

## 解决的科学问题与贡献

{summary['scientific_problem_solved']}

## 局限性

{summary['limitations']}

## 证据定位

{md_list(evidence)}

## 链接

- [在 Zotero 中打开](zotero://select/library/items/{key})
"""
    if pdf and pdf.get("key"):
        automatic += f"- [在 Zotero 中打开 PDF](zotero://open-pdf/library/items/{pdf['key']})\n"
    automatic += f"\n{AUTO_END}"

    if existing and AUTO_END in existing:
        tail = existing.split(AUTO_END, 1)[1]
    elif existing:
        tail = "\n\n## 原有内容（已保留）\n\n" + existing
    else:
        tail = "\n\n## 我的笔记\n\n在这里记录你的批注、联想和后续问题。\n"
    return "\n".join(frontmatter) + "\n" + automatic + "\n\n" + tail.lstrip("\n")


def ensure_library_collection_column(vault: Path) -> None:
    base_path = paths(vault)["base"]
    if not base_path.exists():
        return
    content = base_path.read_text(encoding="utf-8-sig")
    changed = False
    if "\n  zotero_collections:\n" not in content:
        marker = "  scientific_question:\n"
        if marker not in content:
            raise SyncError("Cannot add Zotero collection column: Library.base layout is unfamiliar.")
        content = content.replace(
            marker,
            "  zotero_collections:\n    displayName: Zotero分类\n" + marker,
            1,
        )
        changed = True
    if "\n      - zotero_collections\n" not in content:
        marker = "      - scientific_question\n"
        if marker not in content:
            raise SyncError("Cannot add Zotero collection order: Library.base layout is unfamiliar.")
        content = content.replace(marker, "      - zotero_collections\n" + marker, 1)
        changed = True
    if changed:
        atomic_write_text(base_path, content)


def update_note_properties(
    note_path: Path, values: dict[str, Any], remove_names: set[str] | None = None
) -> None:
    if not note_path.is_file():
        return
    content = note_path.read_text(encoding="utf-8-sig")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return
    try:
        frontmatter_end = next(
            index for index in range(1, len(lines)) if lines[index].strip() == "---"
        )
    except StopIteration:
        return

    remove = set(values) | set(remove_names or ())
    old = lines[1:frontmatter_end]
    rewritten: list[str] = []
    insert_at: int | None = None
    index = 0
    while index < len(old):
        match = re.match(r"^([A-Za-z0-9_-]+):", old[index])
        if match and match.group(1) in remove:
            if insert_at is None:
                insert_at = len(rewritten)
            index += 1
            while index < len(old) and (old[index].startswith((" ", "\t")) or not old[index]):
                index += 1
            continue
        if insert_at is None and old[index].startswith("journal:"):
            insert_at = len(rewritten)
        rewritten.append(old[index])
        index += 1

    if insert_at is None:
        insert_at = len(rewritten)
    property_lines: list[str] = []
    for name, value in values.items():
        yaml_property(property_lines, name, value)
    rewritten[insert_at:insert_at] = property_lines
    updated = lines[:1] + rewritten + lines[frontmatter_end:]
    atomic_write_text(note_path, "\n".join(updated) + ("\n" if content.endswith("\n") else ""))


def update_note_collection_tags(note_path: Path, tags: list[str]) -> None:
    update_note_properties(
        note_path,
        {"zotero_collections": tags},
        {"zotero_collection_ancestors"},
    )


def archive_note(
    vault: Path, key: str, state_item: dict[str, Any], reason: str, roots: list[str]
) -> Path | None:
    relative = state_item.get("note_path")
    if not relative:
        return None
    source = (vault / relative).resolve()
    archive_root = paths(vault)["archive"].resolve()
    papers_root = paths(vault)["papers"].resolve()
    if not any(
        source == root or root in source.parents for root in (papers_root, archive_root)
    ):
        raise SyncError(f"Refusing to archive note outside Zotero/Papers or Archive: {source}")
    archive_root.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        state_item["sync_status"] = "archived"
        state_item["archive_reason"] = reason
        state_item["archived_at"] = now_iso()
        state_item["archived_from_roots"] = sorted(set(roots))
        state_item["watched_roots"] = []
        return None
    destination = archive_root / source.name
    if destination.exists() and destination.resolve() != source:
        destination = archive_root / f"{source.stem} [{key}]{source.suffix}"
    if source.resolve() != destination.resolve():
        source.replace(destination)
    update_note_properties(
        destination,
        {
            "sync_status": "archived",
            "archive_reason": reason,
            "archived_at": now_iso(),
        },
    )
    state_item["note_path"] = destination.relative_to(vault).as_posix()
    state_item["sync_status"] = "archived"
    state_item["archive_reason"] = reason
    state_item["archived_at"] = now_iso()
    state_item["archived_from_roots"] = sorted(set(roots))
    state_item["watched_roots"] = []
    return destination


def restore_archived_note(vault: Path, key: str, state_item: dict[str, Any]) -> None:
    if state_item.get("sync_status") != "archived":
        return
    relative = state_item.get("note_path")
    if not relative:
        return
    source = (vault / relative).resolve()
    if not source.is_file():
        return
    papers_root = paths(vault)["papers"].resolve()
    archive_root = paths(vault)["archive"].resolve()
    if not any(
        source == root or root in source.parents for root in (papers_root, archive_root)
    ):
        raise SyncError(f"Refusing to restore note outside Zotero/Papers or Archive: {source}")
    papers_root.mkdir(parents=True, exist_ok=True)
    destination = papers_root / source.name
    if destination.exists() and destination.resolve() != source:
        destination = papers_root / f"{source.stem} [{key}]{source.suffix}"
    if source.resolve() != destination.resolve():
        source.replace(destination)
    state_item["note_path"] = destination.relative_to(vault).as_posix()
    state_item["sync_status"] = "active"
    for name in ("archive_reason", "archived_at", "archived_from_roots"):
        state_item.pop(name, None)


def collection_base_content(category: str) -> str:
    tag = f"#{category}"
    expression = f'zotero_collections.contains({json.dumps(tag, ensure_ascii=False)})'
    lines = [
        "filters:",
        "  and:",
        '    - \'file.inFolder("Zotero/Papers")\'',
        '    - \'file.ext == "md"\'',
        f"    - {yaml_scalar(expression)}",
        "properties:",
        "  file.name:",
        "    displayName: 文献笔记",
        "  title:",
        "    displayName: Title",
        "  publication_year:",
        "    displayName: 发表年份",
        "  authors:",
        "    displayName: Author",
        "  keywords:",
        "    displayName: Keywords",
        "  zotero_collections:",
        "    displayName: Zotero分类",
        "  scientific_question:",
        "    displayName: 科学问题",
        "  datasets:",
        "    displayName: Dataset",
        "  methods:",
        "    displayName: Methods",
        "  main_findings:",
        "    displayName: 主要结论",
        "  scientific_problem_solved:",
        "    displayName: 解决的问题",
        "views:",
        "  - type: table",
        f"    name: {yaml_scalar(category)}",
        "    order:",
        "      - file.name",
        "      - title",
        "      - publication_year",
        "      - authors",
        "      - keywords",
        "      - zotero_collections",
        "      - scientific_question",
        "      - datasets",
        "      - methods",
        "      - main_findings",
        "      - scientific_problem_solved",
        "",
    ]
    return "\n".join(lines)


def collection_tree(
    counts: dict[tuple[str, ...], int], top_entries: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    roots: dict[str, dict[str, Any]] = {}
    for path, count in sorted(counts.items(), key=lambda row: (len(row[0]), row[0])):
        level = roots
        node: dict[str, Any] | None = None
        for part in path:
            node = level.setdefault(part, {"name": part, "count": 0, "children": {}})
            level = node["children"]
        if node is not None:
            node["count"] = count
    for name, entry in top_entries.items():
        if name in roots:
            roots[name]["relative_path"] = entry["relative_path"]
    return [roots[name] for name in sorted(roots, key=str.casefold)]


def update_dashboard_categories(vault: Path, tree: list[dict[str, Any]]) -> None:
    dashboard = paths(vault)["dashboard"]
    content = dashboard.read_text(encoding="utf-8-sig") if dashboard.exists() else "# Zotero 文献库\n"
    navigation = [COLLECTIONS_START, "## 按 Zotero 分类", ""]

    def append_node(node: dict[str, Any], depth: int) -> None:
        prefix = "  " * depth + "- "
        if depth == 0 and node.get("relative_path"):
            label = f"[[{node['relative_path']}|{node['name']}]]"
        else:
            label = node["name"]
        navigation.append(f"{prefix}{label}（{node['count']}）")
        children = node.get("children", {})
        for name in sorted(children, key=str.casefold):
            append_node(children[name], depth + 1)

    if tree:
        for root in tree:
            append_node(root, 0)
    else:
        navigation.append("暂无已同步的 Zotero 分类。")
    navigation.extend([COLLECTIONS_END])
    block = "\n".join(navigation)
    if COLLECTIONS_START in content and COLLECTIONS_END in content:
        before, remainder = content.split(COLLECTIONS_START, 1)
        _, after = remainder.split(COLLECTIONS_END, 1)
        content = before.rstrip() + "\n\n" + block + after
    elif "## 使用方式" in content:
        content = content.replace("## 使用方式", block + "\n\n## 使用方式", 1)
    else:
        content = content.rstrip() + "\n\n" + block + "\n"
    atomic_write_text(dashboard, content)


def update_dashboard_watch_status(
    vault: Path, config: dict[str, Any] | None = None, state: dict[str, Any] | None = None
) -> None:
    config = config or load_config(vault)
    state = state or load_state(vault)
    dashboard = paths(vault)["dashboard"]
    content = dashboard.read_text(encoding="utf-8-sig") if dashboard.exists() else "# Zotero 文献库\n"
    lines = [WATCH_START, "## 同步目录状态", ""]
    watched = config.get("watched_collections") or []
    if watched:
        lines.extend(
            [
                "| Zotero 一级目录 | Zotero 文献数 | 已总结 | 待处理 | 待归档 | 已归档 | 上次核对 |",
                "|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for entry in sorted(watched, key=lambda row: str(row.get("name", "")).casefold()):
            root_key = entry["collection_key"]
            status = state.get("watch_status", {}).get(root_key, {})
            active_count = sum(
                1
                for item in state.get("items", {}).values()
                if root_key in (item.get("watched_roots") or [])
                and item.get("sync_status", "active") == "active"
                and item.get("summary_status") == "completed"
            )
            archived_count = sum(
                1
                for item in state.get("items", {}).values()
                if root_key in (item.get("archived_from_roots") or [])
                and item.get("sync_status") == "archived"
            )
            total = status.get("zotero_count", "—")
            pending = len(status.get("pending_item_keys") or [])
            removal = len(status.get("removal_candidates") or [])
            checked = status.get("last_inventory_at") or "—"
            lines.append(
                f"| {entry.get('name', root_key)} | {total} | {active_count} | "
                f"{pending} | {removal} | {archived_count} | {checked} |"
            )
        lines.extend(
            [
                "",
                "> 待归档项目不会自动永久删除；执行归档后会移动到 `Zotero/Archive`。",
            ]
        )
    else:
        lines.append("尚未登记需要批量同步的 Zotero 一级目录。")
    lines.append(WATCH_END)
    block = "\n".join(lines)
    if WATCH_START in content and WATCH_END in content:
        before, remainder = content.split(WATCH_START, 1)
        _, after = remainder.split(WATCH_END, 1)
        content = before.rstrip() + "\n\n" + block + after
    elif "## 使用方式" in content:
        content = content.replace("## 使用方式", block + "\n\n## 使用方式", 1)
    else:
        content = content.rstrip() + "\n\n" + block + "\n"
    atomic_write_text(dashboard, content)


def refresh_collection_views(vault: Path, state: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = ensure_initialized(vault)
    state = state or load_state(vault)
    resolved["collections"].mkdir(parents=True, exist_ok=True)
    ensure_library_collection_column(vault)

    counts: dict[tuple[str, ...], int] = {}
    for key, item in state.get("items", {}).items():
        if item.get("sync_status", "active") != "active":
            continue
        collections = item.get("collections")
        if collections is None:
            packet = read_json(resolved["processed"] / f"{key}.packet.json", {})
            collections = packet.get("metadata", {}).get("collections", [])
            item["collections"] = collections
        tags = collection_tags(collections or [])
        item["collection_tags"] = tags
        item.pop("collection_ancestors", None)
        note_relative = item.get("note_path")
        if note_relative:
            update_note_collection_tags(vault / note_relative, tags)
        for prefix in collection_prefixes(collections or []):
            counts[prefix] = counts.get(prefix, 0) + 1

    used_names: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    new_files: set[str] = set()
    top_categories = sorted(
        (path[0] for path in counts if len(path) == 1), key=str.casefold
    )
    for category in top_categories:
        stem = safe_filename(category, 100)
        key = stem.casefold()
        if key in used_names and used_names[key] != category:
            digest = hashlib.sha1(category.encode("utf-8")).hexdigest()[:8]
            stem = safe_filename(f"{stem} [{digest}]", 115)
        used_names[stem.casefold()] = category
        destination = resolved["collections"] / f"{stem}.base"
        atomic_write_text(destination, collection_base_content(category))
        relative = destination.relative_to(vault).as_posix()
        new_files.add(relative)
        entries.append(
            {
                "category": category,
                "count": counts[(category,)],
                "relative_path": relative,
            }
        )

    manifest = read_json(resolved["collection_manifest"], {"files": []})
    collection_root = resolved["collections"].resolve()
    for relative in manifest.get("files", []):
        if relative in new_files:
            continue
        candidate = (vault / relative).resolve()
        try:
            candidate.relative_to(collection_root)
        except ValueError:
            continue
        if candidate.suffix == ".base" and candidate.is_file():
            candidate.unlink()

    atomic_write_json(resolved["collection_manifest"], {"files": sorted(new_files)})
    entry_map = {entry["category"]: entry for entry in entries}
    update_dashboard_categories(vault, collection_tree(counts, entry_map))
    update_dashboard_watch_status(vault, load_config(vault), state)
    save_state(vault, state)
    return {"category_count": len(entries), "categories": entries}


def queue_watched_collection(
    vault: Path, entry: dict[str, Any], limit: int | None = None
) -> dict[str, Any]:
    config = load_config(vault)
    state = load_state(vault)
    catalog = collection_catalog(config)
    root_key = entry["collection_key"]
    current_root = catalog.get(root_key)
    if not current_root or current_root.get("parent_key"):
        raise SyncError(
            f"Watched top-level collection no longer exists in Zotero: {entry.get('name', root_key)}"
        )
    entry["name"] = current_root["name"]
    entry["path"] = current_root["path"]
    inventory = collection_inventory(config, catalog, entry)
    current_keys = set(inventory)
    previous_keys = {
        key
        for key, item in state.get("items", {}).items()
        if root_key in (item.get("watched_roots") or [])
    }
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key, item in inventory.items():
        state_item = state.get("items", {}).get(key)
        if state_item is not None:
            roots = list(state_item.get("watched_roots") or [])
            if root_key not in roots:
                roots.append(root_key)
            state_item["watched_roots"] = sorted(set(roots))
        if needs_sync(item, state_item, catalog):
            candidates.append((key, item))

    candidates.sort(
        key=lambda pair: (
            str(pair[1].get("data", pair[1]).get("dateAdded", "")),
            pair[0],
        )
    )
    batch_limit = int(limit if limit is not None else entry.get("batch_size", 5))
    if batch_limit < 1:
        raise SyncError("Batch limit must be at least 1.")
    path_map = {key: row["path"] for key, row in catalog.items()}
    queued: list[dict[str, Any]] = []
    for key, item in candidates[:batch_limit]:
        output = paths(vault)["pending"] / f"{key}.json"
        if output.exists():
            packet = read_json(output, {})
        else:
            packet = make_packet(item, config, path_map)
            if packet is None:
                continue
            atomic_write_json(output, packet)
        queued.append(
            {
                "item_key": key,
                "title": packet.get("metadata", {}).get("title"),
                "available_summary_basis": packet.get("available_summary_basis"),
                "fulltext_status": packet.get("fulltext", {}).get("status"),
                "packet": str(output),
            }
        )

    removal_candidates = sorted(previous_keys - current_keys)
    state.setdefault("watch_status", {})[root_key] = {
        "name": entry["name"],
        "zotero_count": len(current_keys),
        "pending_item_keys": [key for key, _ in candidates],
        "removal_candidates": removal_candidates,
        "last_inventory_at": now_iso(),
    }
    save_config(vault, config)
    save_state(vault, state)
    update_dashboard_watch_status(vault, config, state)
    return {
        "collection": entry["name"],
        "collection_key": root_key,
        "zotero_count": len(current_keys),
        "already_current": len(current_keys) - len(candidates),
        "sync_needed": len(candidates),
        "queued_count": len(queued),
        "remaining_after_batch": max(0, len(candidates) - len(queued)),
        "removal_candidates": removal_candidates,
        "queued": queued,
    }


def command_collections(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    catalog = collection_catalog(config)
    watched_keys = {
        row.get("collection_key") for row in (config.get("watched_collections") or [])
    }
    emit(
        {
            "top_level_count": len(top_level_collections(catalog)),
            "collections": [
                {
                    "name": row["name"],
                    "collection_key": row["key"],
                    "watched": row["key"] in watched_keys,
                }
                for row in top_level_collections(catalog)
            ],
        }
    )


def command_watch(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    if args.batch_size < 1:
        raise SyncError("Batch size must be at least 1.")
    config = load_config(vault)
    catalog = collection_catalog(config)
    target = resolve_top_collection(catalog, args.collection)
    watched = config.setdefault("watched_collections", [])
    entry = next(
        (row for row in watched if row.get("collection_key") == target["key"]), None
    )
    created = entry is None
    if entry is None:
        entry = {}
        watched.append(entry)
    entry.update(
        {
            "collection_key": target["key"],
            "name": target["name"],
            "path": target["path"],
            "include_descendants": True,
            "batch_size": args.batch_size,
            "removal_policy": "archive",
        }
    )
    save_config(vault, config)
    state = load_state(vault)
    state.setdefault("watch_status", {}).setdefault(target["key"], {"name": target["name"]})
    save_state(vault, state)
    update_dashboard_watch_status(vault, config, state)
    emit(
        {
            "ok": True,
            "created": created,
            "collection": target["name"],
            "collection_key": target["key"],
            "include_descendants": True,
            "batch_size": args.batch_size,
            "removal_policy": "archive",
        }
    )


def command_unwatch(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    entry = watched_entry(config, args.collection)
    root_key = entry["collection_key"]
    config["watched_collections"] = [
        row
        for row in (config.get("watched_collections") or [])
        if row.get("collection_key") != root_key
    ]
    state = load_state(vault)
    state.get("watch_status", {}).pop(root_key, None)
    for item in state.get("items", {}).values():
        item["watched_roots"] = [
            key for key in (item.get("watched_roots") or []) if key != root_key
        ]
    save_config(vault, config)
    save_state(vault, state)
    update_dashboard_watch_status(vault, config, state)
    emit(
        {
            "ok": True,
            "collection": entry.get("name", root_key),
            "collection_key": root_key,
            "notes_preserved": True,
        }
    )


def command_backfill(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    entry = watched_entry(config, args.collection)
    emit({"ok": True, **queue_watched_collection(vault, entry, args.limit)})


def command_sync_watched(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    if args.collection:
        entries = [watched_entry(config, args.collection)]
    else:
        entries = config.get("watched_collections") or []
    if not entries:
        raise SyncError("No watched Zotero collections are configured.")
    results = [queue_watched_collection(vault, entry, args.limit) for entry in entries]
    emit({"ok": True, "collection_count": len(results), "results": results})


def command_reconcile(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    state = load_state(vault)
    catalog = collection_catalog(config)
    if args.collection:
        entries = [watched_entry(config, args.collection)]
    else:
        entries = config.get("watched_collections") or []
    if not entries:
        raise SyncError("No watched Zotero collections are configured.")

    results: list[dict[str, Any]] = []
    changed = False
    for entry in entries:
        root_key = entry["collection_key"]
        inventory = collection_inventory(config, catalog, entry)
        current_keys = set(inventory)
        previous_keys = {
            key
            for key, item in state.get("items", {}).items()
            if root_key in (item.get("watched_roots") or [])
        }
        candidates = sorted(previous_keys - current_keys)
        actions: list[dict[str, Any]] = []
        for key in candidates:
            state_item = state.get("items", {}).get(key)
            if not state_item:
                continue
            processed = read_json(paths(vault)["processed"] / f"{key}.packet.json", {})
            title = processed.get("metadata", {}).get("title") or Path(
                state_item.get("note_path", key)
            ).stem
            live_item, _ = api_get_optional(f"{library_prefix(config)}/items/{key}")
            if isinstance(live_item, dict):
                active_roots = watched_roots_for_item(live_item, catalog, config)
                reason = "moved_out_of_watched_collections"
            else:
                active_roots = []
                reason = "deleted_from_zotero"
            action = "keep_in_other_watched_collection" if active_roots else "archive"
            destination: str | None = None
            if args.apply:
                if active_roots:
                    state_item["watched_roots"] = active_roots
                    state_item["collections"] = mapped_item_collections(live_item, catalog)
                    state_item["collection_tags"] = collection_tags(state_item["collections"])
                    note_relative = state_item.get("note_path")
                    if note_relative:
                        update_note_collection_tags(
                            vault / note_relative, state_item["collection_tags"]
                        )
                else:
                    prior_roots = list(state_item.get("watched_roots") or [root_key])
                    archived = archive_note(vault, key, state_item, reason, prior_roots)
                    destination = str(archived) if archived else None
                changed = True
            actions.append(
                {
                    "item_key": key,
                    "title": title,
                    "reason": reason,
                    "action": action,
                    "destination": destination,
                }
            )

        pending_keys = [
            key
            for key, item in inventory.items()
            if needs_sync(item, state.get("items", {}).get(key), catalog)
        ]
        state.setdefault("watch_status", {})[root_key] = {
            "name": entry.get("name", root_key),
            "zotero_count": len(current_keys),
            "pending_item_keys": pending_keys,
            "removal_candidates": [] if args.apply else candidates,
            "last_inventory_at": now_iso(),
        }
        results.append(
            {
                "collection": entry.get("name", root_key),
                "collection_key": root_key,
                "candidate_count": len(candidates),
                "applied": bool(args.apply),
                "actions": actions,
            }
        )

    save_state(vault, state)
    if args.apply and changed:
        refresh_collection_views(vault, state)
    else:
        update_dashboard_watch_status(vault, config, state)
    emit({"ok": True, "applied": bool(args.apply), "results": results})


def command_init(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    resolved = paths(vault)
    for name in ("papers", "archive", "reviews", "collections", "pending", "processed"):
        resolved[name].mkdir(parents=True, exist_ok=True)
    asset_dir = Path(__file__).resolve().parent.parent / "assets"
    copies = {
        asset_dir / "Library.base": resolved["base"],
        asset_dir / "Zotero Dashboard.md": resolved["dashboard"],
        asset_dir / "config.json": resolved["config"],
    }
    created: list[str] = []
    preserved: list[str] = []
    for source, destination in copies.items():
        if destination.exists() and not args.force:
            preserved.append(str(destination))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        created.append(str(destination))
    if not resolved["state"].exists():
        save_state(vault, default_state())
        created.append(str(resolved["state"]))
    emit({"ok": True, "vault": str(vault), "created": created, "preserved": preserved})


def command_probe(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    info = server_info()
    version, server_id = current_library_version(config)
    info.update({"library_version": version, "server_id": server_id or info["server_id"]})
    emit(info)


def command_bootstrap(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    state = load_state(vault)
    version, server_id = current_library_version(config)
    if state.get("server_id") and state["server_id"] != server_id and not args.reset:
        check_server(state, server_id)
    if args.reset:
        state = default_state()
    state["server_id"] = server_id
    state["library_version"] = version
    state["last_scan_at"] = now_iso()
    save_state(vault, state)
    emit({"ok": True, "mode": "new_only", "server_id": server_id, "library_version": version})


def command_scan(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    config = load_config(vault)
    state = load_state(vault)
    if state.get("library_version") is None:
        raise SyncError("No baseline exists. Run bootstrap before adding the trial paper.")
    info = server_info()
    check_server(state, info.get("server_id"))
    params = {
        "format": "json",
        "since": int(state["library_version"]),
        "sort": "dateModified",
        "direction": "asc",
    }
    rows, headers = api_get(f"{library_prefix(config)}/items/top", params)
    collections = collection_paths(config)
    queued: list[dict[str, Any]] = []
    ignored = 0
    for item in rows or []:
        packet = make_packet(item, config, collections)
        if not packet:
            ignored += 1
            continue
        output = paths(vault)["pending"] / f"{packet['zotero_item_key']}.json"
        atomic_write_json(output, packet)
        queued.append(
            {
                "item_key": packet["zotero_item_key"],
                "title": packet["metadata"]["title"],
                "available_summary_basis": packet["available_summary_basis"],
                "fulltext_status": packet["fulltext"]["status"],
                "packet": str(output),
            }
        )
    raw_version = header_value(headers, "Last-Modified-Version")
    if raw_version is not None:
        state["library_version"] = int(raw_version)
    state["last_scan_at"] = now_iso()
    save_state(vault, state)
    emit(
        {
            "ok": True,
            "queued_count": len(queued),
            "ignored_count": ignored,
            "library_version": state["library_version"],
            "queued": queued,
        }
    )


def command_fetch(args: argparse.Namespace) -> None:
    output = fetch_packet(vault_path(args.vault), args.item_key)
    packet = read_json(output)
    emit(
        {
            "ok": True,
            "packet": str(output),
            "item_key": packet["zotero_item_key"],
            "title": packet["metadata"]["title"],
            "available_summary_basis": packet["available_summary_basis"],
        }
    )


def command_pending(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    ensure_initialized(vault)
    output = []
    for path in sorted(paths(vault)["pending"].glob("*.json")):
        if path.name.endswith(".summary.json"):
            continue
        packet = read_json(path)
        output.append(
            {
                "item_key": packet.get("zotero_item_key"),
                "title": packet.get("metadata", {}).get("title"),
                "available_summary_basis": packet.get("available_summary_basis"),
                "fulltext_status": packet.get("fulltext", {}).get("status"),
                "packet": str(path),
            }
        )
    emit({"pending_count": len(output), "items": output})


def command_render(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    ensure_initialized(vault)
    packet_path = Path(args.packet).resolve()
    summary_path = Path(args.summary).resolve()
    packet = read_json(packet_path)
    summary = read_json(summary_path)
    if not isinstance(packet, dict) or not isinstance(summary, dict):
        raise SyncError("Packet and summary must be JSON objects")
    validate_summary(summary, packet)
    state = load_state(vault)
    previous_state = state.get("items", {}).get(packet["zotero_item_key"], {})
    restore_archived_note(vault, packet["zotero_item_key"], previous_state)
    note_path = note_path_for(vault, packet, state)
    existing = note_path.read_text(encoding="utf-8-sig") if note_path.exists() else ""
    atomic_write_text(note_path, build_note(packet, summary, existing))

    key = packet["zotero_item_key"]
    watched_roots = set(previous_state.get("watched_roots") or [])
    for root_key, status in state.get("watch_status", {}).items():
        if key in (status.get("pending_item_keys") or []):
            watched_roots.add(root_key)
    config = load_config(vault)
    collection_roots = {
        str(path).split("/", 1)[0]
        for path in (packet.get("metadata", {}).get("collections") or [])
        if path
    }
    for entry in config.get("watched_collections") or []:
        if entry.get("name") in collection_roots:
            watched_roots.add(entry["collection_key"])
    rendered_state = {
        "note_path": note_path.relative_to(vault).as_posix(),
        "source_version": packet.get("source_version", 0),
        "summary_basis": summary["summary_basis"],
        "summary_status": "completed",
        "sync_status": "active",
        "collections": packet.get("metadata", {}).get("collections", []),
        "collection_tags": collection_tags(packet.get("metadata", {}).get("collections", [])),
        "watched_roots": sorted(watched_roots),
        "last_synced": now_iso(),
    }
    for name in ("review_path", "review_updated"):
        if name in previous_state:
            rendered_state[name] = previous_state[name]
    state["items"][key] = rendered_state
    for status in state.get("watch_status", {}).values():
        status["pending_item_keys"] = [
            value for value in (status.get("pending_item_keys") or []) if value != key
        ]
    save_state(vault, state)

    processed_packet = dict(packet)
    processed_packet["fulltext"] = dict(packet.get("fulltext", {}))
    processed_packet["fulltext"].pop("content", None)
    processed_packet_path = paths(vault)["processed"] / f"{key}.packet.json"
    processed_summary_path = paths(vault)["processed"] / f"{key}.summary.json"
    atomic_write_json(processed_packet_path, processed_packet)
    atomic_write_json(processed_summary_path, summary)
    collection_result = refresh_collection_views(vault, state)
    if packet_path.parent == paths(vault)["pending"] and packet_path.exists():
        packet_path.unlink()
    if summary_path.parent == paths(vault)["pending"] and summary_path.exists():
        summary_path.unlink()
    emit(
        {
            "ok": True,
            "item_key": key,
            "note": str(note_path),
            "summary_basis": summary["summary_basis"],
            "updated_existing": bool(existing),
            "collection_views": collection_result,
        }
    )


def command_render_review(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    ensure_initialized(vault)
    state = load_state(vault)
    item = state.get("items", {}).get(args.item_key)
    if not item:
        raise SyncError("Render the quick literature note before creating a detailed review.")
    source = Path(args.content).resolve()
    content = source.read_text(encoding="utf-8-sig").strip()
    if not content:
        raise SyncError("Detailed review content is empty")
    paper_rel = item["note_path"]
    paper_stem = Path(paper_rel).stem
    review_path = paths(vault)["reviews"] / f"{paper_stem} - Detailed Review.md"
    lines = ["---"]
    yaml_property(lines, "tags", ["literature-review", "zotero"])
    yaml_property(lines, "zotero_item_key", args.item_key)
    yaml_property(lines, "paper_note", f"[[{Path(paper_rel).as_posix()}]]")
    yaml_property(lines, "last_updated", now_iso())
    lines.append("---")
    rendered = "\n".join(lines) + f"\n# 详细解读：{paper_stem}\n\n{content}\n"
    atomic_write_text(review_path, rendered)
    item["review_path"] = review_path.relative_to(vault).as_posix()
    item["review_updated"] = now_iso()
    save_state(vault, state)
    emit({"ok": True, "item_key": args.item_key, "review": str(review_path)})


def command_status(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    ensure_initialized(vault)
    state = load_state(vault)
    config = load_config(vault)
    emit(
        {
            "server_id": state.get("server_id"),
            "library_version": state.get("library_version"),
            "last_scan_at": state.get("last_scan_at"),
            "synchronized_count": len(state.get("items", {})),
            "watched_collections": config.get("watched_collections") or [],
            "watch_status": state.get("watch_status") or {},
            "items": state.get("items", {}),
        }
    )


def command_refresh_collections(args: argparse.Namespace) -> None:
    vault = vault_path(args.vault)
    result = refresh_collection_views(vault)
    emit({"ok": True, **result})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    def common(command: str, handler: Any) -> argparse.ArgumentParser:
        child = sub.add_parser(command)
        child.add_argument("--vault", default=".", help="Obsidian vault root")
        child.set_defaults(handler=handler)
        return child

    init = common("init-vault", command_init)
    init.add_argument("--force", action="store_true", help="replace generated dashboard/base/config")
    common("probe", command_probe)
    bootstrap = common("bootstrap", command_bootstrap)
    bootstrap.add_argument("--reset", action="store_true", help="discard sync versions for a confirmed new Zotero database")
    common("scan", command_scan)
    common("collections", command_collections)
    watch = common("watch", command_watch)
    watch.add_argument("--collection", required=True, help="top-level collection name or key")
    watch.add_argument("--batch-size", type=int, default=5)
    unwatch = common("unwatch", command_unwatch)
    unwatch.add_argument("--collection", required=True, help="watched collection name or key")
    backfill = common("backfill", command_backfill)
    backfill.add_argument("--collection", help="watched collection name or key")
    backfill.add_argument("--limit", type=int, help="maximum papers to queue in this batch")
    sync_watched = common("sync-watched", command_sync_watched)
    sync_watched.add_argument("--collection", help="watched collection name or key")
    sync_watched.add_argument("--limit", type=int, help="maximum papers per collection")
    reconcile = common("reconcile", command_reconcile)
    reconcile.add_argument("--collection", help="watched collection name or key")
    reconcile.add_argument(
        "--apply",
        action="store_true",
        help="move removal candidates to Zotero/Archive; without this flag only preview",
    )
    fetch = common("fetch", command_fetch)
    fetch.add_argument("--item-key", required=True)
    common("pending", command_pending)
    render = common("render", command_render)
    render.add_argument("--packet", required=True)
    render.add_argument("--summary", required=True)
    review = common("render-review", command_render_review)
    review.add_argument("--item-key", required=True)
    review.add_argument("--content", required=True)
    common("status", command_status)
    common("refresh-collections", command_refresh_collections)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
        return 0
    except (SyncError, json.JSONDecodeError, OSError, ValueError) as exc:
        emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
