"""Validate model citations against the confirmed immutable job snapshot."""


def normalize_evidence(output, messages):
    sources = {row["uid"]: row["text"] for row in messages
               if isinstance(row, dict) and isinstance(row.get("uid"), str)
               and isinstance(row.get("text"), str)}
    rows = output.get("evidence")
    if not isinstance(rows, list) or not rows:
        rows = [{"kind": kind, "text": text} for kind, key in
                (("fact", "facts"), ("hypothesis", "hypotheses"), ("unknown", "unknowns"))
                for text in (output.get(key) if isinstance(output.get(key), list) else [])[:12]
                if isinstance(text, str)]
    result, counts = [], {"fact": 0, "hypothesis": 0, "unknown": 0}
    for row in rows[:36]:
        if not isinstance(row, dict):
            continue
        kind, text = row.get("kind"), row.get("text")
        if kind not in counts or not isinstance(text, str) or not text.strip() or counts[kind] >= 12:
            continue
        counts[kind] += 1
        quotes, identifiers = [], []
        raw = row.get("source_quotes")
        for item in (raw if isinstance(raw, list) else [])[:16]:
            if not isinstance(item, dict):
                continue
            uid, quote = item.get("message_uid"), item.get("quote")
            if (isinstance(uid, str) and uid in sources and isinstance(quote, str)
                    and quote.strip() and len(quote) <= 1000 and quote in sources[uid]):
                value = {"message_uid": uid, "quote": quote}
                if value not in quotes:
                    quotes.append(value)
                if uid not in identifiers:
                    identifiers.append(uid)
        result.append({"claim_id": f"{kind}-{counts[kind]}", "kind": kind,
            "text": text.strip()[:2000], "source_message_uids": identifiers,
            "source_quotes": quotes, "citation_status": "verified" if quotes else "unavailable"})
    return result
