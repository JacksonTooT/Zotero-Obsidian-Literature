---
name: zotero-obsidian-literature
description: Sync new or changed Zotero papers into structured Obsidian literature notes, create evidence-grounded Chinese summaries, maintain an Obsidian Bases library view, and produce detailed paper reviews on request. Use for Zotero-to-Obsidian synchronization, literature-note generation, paper summarization, or rebuilding the literature dashboard. Do not modify or delete Zotero items.
---

# Zotero Obsidian Literature

Use the bundled script for deterministic Zotero discovery, state tracking, and note rendering. Use model judgment only for scientific interpretation and summary fields.

## Safety and invariants

- Treat Zotero as read-only. Never edit its database or items.
- Never infer the Obsidian vault from the current working directory, a workspace-local test/prototype vault, or the presence of a `Zotero` folder alone.
- Identify papers by `zotero_item_key`, never by title alone.
- Never invent datasets, methods, findings, or scientific contributions. Use `文中未明确说明` when evidence is absent.
- State whether a summary is based on full text, abstract, or metadata.
- Preserve everything after `<!-- codex:auto:end -->` in an existing paper note.
- Do not process items tagged `no-ai` or `codex-ignore`.

## Confirm the Obsidian vault first

Resolve the destination vault before running any command that writes files or state.

1. If the user has not explicitly provided or confirmed the Obsidian vault root in the current conversation, ask: `你的 Obsidian 默认仓库目录是什么？请提供包含 .obsidian 文件夹的 vault 根目录。` Stop and wait for the answer; do not initialize, scan, watch, backfill, render, or copy notes meanwhile.
2. Validate the supplied path read-only. The target vault root must contain `.obsidian` directly.
3. If the supplied path is only a parent directory, or it contains multiple candidate child directories with `.obsidian`, show the candidates and ask the user to confirm the exact vault root. Do not select one by guesswork.
4. Once confirmed, use that exact absolute path consistently for every `--vault` argument and delivered file link in the task.
5. Before the first write, run `status` or `probe` against the confirmed vault. Preserve any existing notes, watched collections, and synchronization state. If its saved Zotero `server_id` differs from the active Zotero server, follow the existing mismatch stop rule.

## First-time setup

Only after the vault root has been confirmed and validated, run from that Obsidian vault root:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py init-vault --vault .
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py probe --vault .
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py bootstrap --vault .
```

`bootstrap` records the current Zotero library version without importing existing papers. Run it before adding the trial paper.

## Sync new or changed papers

1. Run `zotero_sync.py scan --vault <vault>`.
2. Run `zotero_sync.py pending --vault <vault>` and process each returned packet.
3. Read [references/summary-schema.md](references/summary-schema.md) and [references/summary-rubric.md](references/summary-rubric.md).
4. Read the packet metadata, abstract, and available full text. Create one UTF-8 JSON summary matching the schema beside the packet, with suffix `.summary.json`.
5. Render it:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py render --vault <vault> --packet <packet.json> --summary <packet.summary.json>
```

6. Rendering also refreshes the total-table collection column, generated collection Bases, and Dashboard category navigation. Store every Zotero path segment in `zotero_collections` as an ordered `#`-prefixed label. For example, `环境遥感/植被遥感/植被指数` becomes `#环境遥感`, `#植被遥感`, `#植被指数`.
7. Generate one Base only for each top-level Zotero collection. A top-level Base includes papers filed anywhere beneath that branch. Show lower collection levels as nested, non-Base entries in the Dashboard, with counts that include their descendants.
8. Report each created or updated note, its evidence basis, and any missing full text. A PDF attachment with unavailable indexed text should be reported as waiting for Zotero indexing; do not imply a full-text review.

## Detailed review on request

1. Resolve the paper from its item key or an existing note's `zotero_item_key`.
2. Run `fetch --item-key <key>` to refresh metadata and full text.
3. Read [references/detailed-review.md](references/detailed-review.md).
4. Write the detailed review Markdown to a temporary UTF-8 file.
5. Run `render-review --item-key <key> --content <review.md>`.

## Managed collection backfill

Use managed collections when the user wants to import papers that existed before the new-only baseline or repeatedly synchronize an entire top-level Zotero branch. Read [references/managed-collections.md](references/managed-collections.md) before registering, backfilling, synchronizing, or reconciling a collection.

- Register only top-level Zotero collections; descendants are included automatically.
- Default to batches of five papers so each full-text summary remains reviewable and resumable.
- The Dashboard sync-status table is generated output, not a configuration input.
- Preview removals before applying them. Applying reconciliation moves notes to `Zotero/Archive`; it never permanently deletes them.

## Status and troubleshooting

- `probe` checks whether Zotero's local API is reachable.
- `status` prints the saved server/library version and synchronized items.
- `pending` lists packets not yet rendered.
- `collections` lists available top-level Zotero collections and stable collection keys.
- `watch`, `backfill`, and `sync-watched` manage historical and ongoing collection-scoped synchronization.
- `reconcile` previews moved or deleted items; `reconcile --apply` archives their Obsidian notes only after the user requests that action.
- `refresh-collections` rebuilds generated collection views and Dashboard navigation without moving or duplicating paper notes.
- A connection refusal usually means Zotero is closed or its local API is disabled in Settings → Advanced.
- If Zotero reports a different server ID, stop and ask before using `bootstrap --reset`; cached versions belong to a different Zotero database.
