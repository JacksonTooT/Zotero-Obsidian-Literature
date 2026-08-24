# Managed collection workflow

Use a stable Zotero collection key internally even when the user names a collection in natural language. Zotero remains read-only throughout this workflow.

## Discover and register

List top-level collections when the requested name is uncertain:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py collections --vault <vault>
```

Register one top-level branch. Descendants are always included, and the default removal policy is recoverable archiving:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py watch --vault <vault> --collection <name-or-key> --batch-size 5
```

Do not register a similarly named collection by guesswork. If resolution is ambiguous, show the matching keys and ask the user to choose.

## Backfill historical papers

Queue one bounded batch:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py backfill --vault <vault> --collection <name-or-key> --limit 5
```

Then process only the returned packets using the normal summary schema and `render` workflow. Report how many papers remain. Do not silently continue through unlimited batches; continue when the user asks or when their original request explicitly covers the full collection.

## Synchronize registered branches

Queue new, changed, moved, or still-unprocessed papers across all registered branches:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py sync-watched --vault <vault> --limit 5
```

Pass `--collection <name-or-key>` to restrict the run to one branch. Process the queued packets normally. Dashboard status is refreshed automatically.

## Reconcile removals safely

Preview first:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py reconcile --vault <vault> --collection <name-or-key>
```

Explain whether each item was deleted from Zotero or merely moved outside all registered branches. If the user requested archiving, apply the previewed actions:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py reconcile --vault <vault> --collection <name-or-key> --apply
```

Applying reconciliation moves the Markdown note to `Zotero/Archive`, preserves its manual-note section, marks its archive reason, and removes it from active Bases. If the paper later returns to a watched branch, rendering restores it to `Zotero/Papers`. Never permanently delete archived notes without a separate explicit request that identifies the targets.

Removing a branch from the watch list does not alter or archive existing notes:

```powershell
python .agents/skills/zotero-obsidian-literature/scripts/zotero_sync.py unwatch --vault <vault> --collection <name-or-key>
```
