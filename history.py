"""Read-only activity history for one address, worked out from the chain.

Used by the node's GET /history and the web wallet's GET /api/history.
Nothing here changes consensus. Test coins have no value."""
from core import tx_id

MAX_LIMIT = 50


def _items_in_block(block, addr):
    """Activity items for `addr` in one block (a Block object or a block dict), newest last."""
    get = (lambda k: block.get(k)) if isinstance(block, dict) else (lambda k: getattr(block, k))
    height, ts = int(get("index")), float(get("timestamp"))
    out = []
    for tx in get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        kind = tx.get("type")
        if kind == "reward":
            if tx.get("recipient") == addr:
                out.append({"kind": "mined", "amount": int(tx.get("amount", 0)) + int(tx.get("fees", 0) or 0)})
        elif kind == "payout":
            if tx.get("recipient") == addr:
                out.append({"kind": "reward_share", "amount": int(tx.get("amount", 0)),
                            "epoch": int(tx.get("epoch", 0))})
        elif kind == "allocation":
            if tx.get("recipient") == addr:
                out.append({"kind": "allocation", "amount": int(tx.get("amount", 0))})
        elif kind is None and isinstance(tx.get("payload"), dict):
            p = tx["payload"]
            s, r = str(p.get("sender") or ""), str(p.get("recipient") or "")
            if addr not in (s, r):
                continue
            try:
                tid = tx_id(tx)
            except Exception:
                tid = ""
            amt, fee = int(p.get("amount", 0)), int(p.get("fee", 0) or 0)
            if s == addr:
                out.append({"kind": "sent", "amount": amt, "fee": fee, "to": r, "tx_id": tid})
            elif r == addr:
                out.append({"kind": "received", "amount": amt, "from": s, "tx_id": tid})
    for it in out:
        it["height"], it["timestamp"] = height, ts
    return out


def address_history(blocks, addr, offset=0, limit=20):
    """Newest first. Returns {"items": [...], "next_offset": int|None, "total": int}."""
    offset = max(0, int(offset))
    limit = max(1, min(MAX_LIMIT, int(limit)))
    items = []
    for block in reversed(blocks):
        items.extend(reversed(_items_in_block(block, addr)))
    page = items[offset:offset + limit]
    nxt = offset + limit if offset + limit < len(items) else None
    return {"items": page, "next_offset": nxt, "total": len(items)}
