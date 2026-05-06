# mbox-style

Convert an `.mbox` inbox export into datasets for local writing-style fine-tuning.

## Use with `uv`

```bash
uv run mbox_to_dataset.py /path/to/inbox.mbox --out-dir output --author-email you@domain.com
```

or via installed script:

```bash
uv sync
uv run mbox-to-dataset /path/to/inbox.mbox --out-dir output --author-email you@domain.com
```

## Important: identifying the speaker (you)

An mbox alone does **not always** unambiguously identify which sender is you. This tool lets you specify your addresses with `--author-email` (repeatable) and marks messages as `is_author=true` when the `From` matches.

If `--author-email` is provided:
- style datasets include only your authored emails.

If omitted:
- style datasets include all filtered emails (less precise for personal style training).

## Outputs

- `emails_raw.jsonl` (all filtered emails + metadata)
- `style_text.jsonl` (`{"text": ...}` records from author-only messages when provided)
- `instruction_email_style.jsonl` (`{"prompt": ..., "response": ...}`)
- `thread_reply_pairs.jsonl` (multi-turn pairs where someone else writes, then you reply)
- `emails_dataset.csv`
- `summary.json`

## Multi-turn behavior

Yes: the tool now produces `thread_reply_pairs.jsonl` by grouping messages into threads via `References`/`In-Reply-To`, sorting by date, and extracting adjacent turns where `previous.is_author == false` and `current.is_author == true`.

This gives reply-style training examples (incoming message -> your response).

## Example

```bash
uv run mbox_to_dataset.py ~/mail/inbox.mbox \
  --out-dir output \
  --author-email you@domain.com \
  --author-email alias@domain.com \
  --min-chars 100 \
  --max-chars 5000
```
