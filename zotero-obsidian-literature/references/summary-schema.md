# Quick summary JSON schema

Create one JSON object with exactly these fields:

```json
{
  "summary_basis": "fulltext",
  "keywords": [],
  "brief_summary": "",
  "scientific_question": "",
  "datasets": [],
  "methods": [],
  "main_findings": "",
  "scientific_problem_solved": "",
  "limitations": "",
  "evidence_notes": []
}
```

Rules:

- `summary_basis` must be `fulltext`, `abstract`, or `metadata` and must match the packet.
- `keywords` contains author-provided keywords found in the paper. If none are available, use `[]`; do not invent topical labels.
- All prose is concise Chinese. Keep `brief_summary` within about 160 Chinese characters.
- `keywords`, `datasets`, and `methods` are arrays of short names. Use `["文中未明确说明"]` for missing datasets or methods rather than guessing.
- `evidence_notes` is a short list of section/page cues when available. Do not fabricate page numbers.
- JSON must be UTF-8 and contain no Markdown fences.
