GHOST = 900000

def ghost_id(staging_id: int) -> int:
    return GHOST + int(staging_id)

def as_lot(row: dict) -> dict:
    sid = int(row["id"])
    return {
        "id": ghost_id(sid),
        "item_id": row["item_id"],
        "qty_in": row.get("qty"),
        "qty_remain": row.get("qty"),
        "expiry": row.get("expiry"),
        "status": "on_shelf",
        "data_quality": "clean",
        "name": row.get("name"),
        "layer": row.get("layer"),
        "unit": row.get("unit"),
        "staging_id": sid,
    }

def load_confirmed(c) -> list[dict]:
    return [dict(s) for s in c.execute(
        """SELECT staging_lots.*, items.name, items.layer, items.unit
           FROM staging_lots JOIN items ON items.id=staging_lots.item_id
           WHERE staging_lots.status='confirmed'""")]

def mix_fridge(rows: list[dict], confirmed: list[dict], layer=None) -> list[dict]:
    out = list(rows)
    for s in confirmed:
        lot = as_lot(s)
        if (not layer) or lot.get("layer") == layer:
            out.append(lot)
    return out

def mix_consume(lots: list[dict], confirmed: list[dict]) -> list[dict]:
    return list(lots) + [as_lot(s) for s in confirmed]

def eligible_after_warn(lots, warn, today, default_fn):
    if int(warn) == 3:
        return default_fn(lots, today)
    return [x for x in lots if float(x.get("qty_remain") or 0) > 0]

def sweep_ids(lots, warn, today, default_fn):
    if int(warn) == 3:
        return default_fn(lots, today)
    from datetime import date, timedelta
    cut = (date.today() + timedelta(days=int(warn))).isoformat()
    return [l["id"] for l in lots if l.get("expiry") and l["expiry"] < cut]

def alerts_without_ghosts(rows: list[dict]) -> list[dict]:
    return [r for r in rows if int(r.get("id") or 0) < GHOST]
