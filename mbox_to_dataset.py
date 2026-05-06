#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mailbox
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Iterable

RE_QUOTE_PREFIX = re.compile(r"^>+\s?", re.MULTILINE)
RE_SIGNATURE = re.compile(r"\n--\s*\n.*$", re.DOTALL)
RE_WHITESPACE = re.compile(r"[ \t]+")


@dataclass
class EmailRecord:
    message_id: str
    thread_id: str
    date_iso: str
    subject: str
    from_addr: str
    to_addr: str
    is_author: bool
    body: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parse mbox and export fine-tuning datasets")
    p.add_argument("mbox_path", type=Path)
    p.add_argument("--out-dir", type=Path, default=Path("output"))
    p.add_argument("--author-email", action="append", default=[], help="Your email address; repeat flag for aliases")
    p.add_argument("--min-chars", type=int, default=80)
    p.add_argument("--max-chars", type=int, default=6000)
    p.add_argument("--instruction-template", default="Write an email in this style about: {topic}")
    return p.parse_args()


def extract_text(msg: Message) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = part.get("Content-Disposition", "")
            if ctype == "text/plain" and "attachment" not in cdisp:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = RE_QUOTE_PREFIX.sub("", text)
    text = RE_SIGNATURE.sub("", text)
    text = RE_WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def parse_date(raw_date: str | None) -> str:
    if not raw_date:
        return ""
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def thread_id(msg: Message) -> str:
    refs = (msg.get("References") or "").strip()
    if refs:
        first = refs.split()[0]
        return first.strip("<>")
    irt = (msg.get("In-Reply-To") or "").strip()
    if irt:
        return irt.strip("<>")
    mid = msg.get("Message-ID")
    if mid:
        return mid.strip("<>")
    return ""


def normalized_addresses(raw: str) -> set[str]:
    return {addr.lower() for _, addr in getaddresses([raw]) if addr}


def is_author_message(from_header: str, author_emails: set[str]) -> bool:
    from_addrs = normalized_addresses(from_header)
    return bool(from_addrs & author_emails)


def iter_records(mbox_path: Path, author_emails: set[str]) -> Iterable[EmailRecord]:
    inbox = mailbox.mbox(str(mbox_path))
    for msg in inbox:
        body = normalize_text(extract_text(msg))
        mid = (msg.get("Message-ID") or "").strip("<>") or hashlib.sha256(body.encode()).hexdigest()[:16]
        yield EmailRecord(
            message_id=mid,
            thread_id=thread_id(msg) or mid,
            date_iso=parse_date(msg.get("Date")),
            subject=(msg.get("Subject") or "").strip(),
            from_addr=(msg.get("From") or "").strip(),
            to_addr=(msg.get("To") or "").strip(),
            is_author=is_author_message(msg.get("From", ""), author_emails),
            body=body,
        )


def topic_from_subject(subject: str) -> str:
    return re.sub(r"^(re|fwd):\s*", "", subject, flags=re.IGNORECASE).strip() or "a relevant topic"


def to_turn_pairs(records: list[EmailRecord]) -> list[dict[str, str]]:
    threads: dict[str, list[EmailRecord]] = {}
    for r in records:
        threads.setdefault(r.thread_id, []).append(r)
    for group in threads.values():
        group.sort(key=lambda x: x.date_iso or "")

    pairs: list[dict[str, str]] = []
    for tid, msgs in threads.items():
        for prev, cur in zip(msgs, msgs[1:]):
            if cur.is_author and not prev.is_author:
                pairs.append({
                    "thread_id": tid,
                    "prompt": prev.body,
                    "response": cur.body,
                    "prompt_subject": prev.subject,
                    "response_subject": cur.subject,
                })
    return pairs


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    author_emails = {a.lower() for a in args.author_email}

    all_records = list(iter_records(args.mbox_path, author_emails))
    filtered = [r for r in all_records if args.min_chars <= len(r.body) <= args.max_chars and len(r.body.split()) >= 20]
    authored = [r for r in filtered if r.is_author] if author_emails else filtered

    (args.out_dir / "emails_raw.jsonl").write_text(
        "".join(json.dumps(r.__dict__, ensure_ascii=False) + "\n" for r in filtered), encoding="utf-8"
    )
    (args.out_dir / "style_text.jsonl").write_text(
        "".join(json.dumps({"text": r.body}, ensure_ascii=False) + "\n" for r in authored), encoding="utf-8"
    )
    (args.out_dir / "instruction_email_style.jsonl").write_text(
        "".join(
            json.dumps(
                {"prompt": args.instruction_template.format(topic=topic_from_subject(r.subject)), "response": r.body},
                ensure_ascii=False,
            )
            + "\n"
            for r in authored
        ),
        encoding="utf-8",
    )

    pairs = to_turn_pairs(filtered)
    (args.out_dir / "thread_reply_pairs.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pairs), encoding="utf-8"
    )

    with (args.out_dir / "emails_dataset.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["message_id", "thread_id", "date_iso", "subject", "from_addr", "to_addr", "is_author", "body"],
        )
        writer.writeheader()
        for r in filtered:
            writer.writerow(r.__dict__)

    summary = {
        "input_messages": len(all_records),
        "dataset_messages": len(filtered),
        "author_emails_provided": sorted(author_emails),
        "author_messages_used_for_style": len(authored),
        "thread_reply_pairs": len(pairs),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
