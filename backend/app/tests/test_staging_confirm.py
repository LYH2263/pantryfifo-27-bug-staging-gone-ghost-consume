"""落层确认闸门的验收口径:

- 成功: 待落空、在架多一行真实主键的行,全层与层页对上同一笔。
- 失败(非正数量/暂存已空): lots 不增行,待落条数停在点下去之前。
- 顶条、收走、先到期共用同一套过期判定(expiry < today),与 warn_days 无关。
- 确认只新增 lots 行,脏种与负余量种行不被洗成干净。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app

TODAY = date.today()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
SOON5 = (TODAY + timedelta(days=5)).isoformat()
FUTURE = (TODAY + timedelta(days=30)).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def lots_rows():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM lots ORDER BY id")]
    c.close()
    return rows


def set_warn(days):
    c = connect()
    c.execute("UPDATE settings SET value=? WHERE key='warn_days'", (str(days),))
    c.commit()
    c.close()


def stage_and_confirm(client, item_id, qty, expiry):
    sid = client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry}).json()["id"]
    resp = client.post(f"/api/staging/{sid}/confirm")
    assert resp.status_code == 200
    return sid, resp.json()["lot_id"]


def expected_expired_ids():
    today = date.today().isoformat()
    return {
        r["id"] for r in lots_rows()
        if r["status"] == "on_shelf" and r["qty_remain"] > 0 and r["expiry"] and r["expiry"] < today
    }


def test_confirm_success_empties_staging_and_adds_one_real_lot(client):
    lots_before = len(lots_rows())
    sid = client.post("/api/lots", json={"item_id": 1, "qty": 5, "expiry": FUTURE}).json()["id"]
    assert len(client.get("/api/staging").json()) == 1

    resp = client.post(f"/api/staging/{sid}/confirm")
    assert resp.status_code == 200
    lot_id = resp.json()["lot_id"]
    assert lot_id < 900000  # 真实主键,不是幽灵号

    assert client.get("/api/staging").json() == []  # 待落空
    assert len(lots_rows()) == lots_before + 1  # 在架恰好多一行

    all_rows = client.get("/api/fridge").json()
    layer_rows = client.get("/api/fridge", params={"layer": "upper"}).json()
    assert lot_id in {x["id"] for x in all_rows}  # 总表见得到
    assert lot_id in {x["id"] for x in layer_rows}  # 该层页见得到,同一笔
    row = next(x for x in all_rows if x["id"] == lot_id)
    assert row["qty_remain"] == 5 and row["expiry"] == FUTURE and row["layer"] == "upper"


def test_confirm_non_positive_qty_keeps_staging_and_lots(client):
    lots_before = len(lots_rows())
    sid = client.post("/api/lots", json={"item_id": 1, "qty": 0, "expiry": FUTURE}).json()["id"]
    assert len(client.get("/api/staging").json()) == 1

    resp = client.post(f"/api/staging/{sid}/confirm")
    assert resp.status_code == 400 and resp.json()["detail"] == "qty_non_positive"
    assert len(client.get("/api/staging").json()) == 1  # 待落条数停在失败前
    assert len(lots_rows()) == lots_before  # 在架不多行


def test_confirm_unknown_or_repeated_is_staging_empty(client):
    assert client.post("/api/staging/999/confirm").status_code == 404

    sid = client.post("/api/lots", json={"item_id": 1, "qty": 2, "expiry": FUTURE}).json()["id"]
    assert client.post(f"/api/staging/{sid}/confirm").status_code == 200
    lots_after_ok = len(lots_rows())

    resp = client.post(f"/api/staging/{sid}/confirm")
    assert resp.status_code == 404 and resp.json()["detail"] == "staging_empty"
    assert len(lots_rows()) == lots_after_ok  # 不会二次落层造出行


def test_expired_confirmed_lot_consistent_across_alerts_sweep_consume(client):
    _, lot_id = stage_and_confirm(client, item_id=2, qty=4, expiry=YESTERDAY)

    # 顶条: 当过期踢出
    alerts = {a["id"]: a for a in client.get("/api/alerts").json()}
    assert alerts[lot_id]["level"] == "expired"

    # 先到期: 过期批不可扣(好批 12 个鸡蛋,要 13 必然 short,且扣不到它)
    resp = client.post("/api/consume", json={"item_id": 2, "qty": 13})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "short"
    assert lot_id not in {d["lot_id"] for d in detail["deductions"]}

    # 收走: 名单里有它,且真扫走
    ids = client.post("/api/expire-sweep").json()["expired_ids"]
    assert lot_id in ids
    assert lot_id not in {x["id"] for x in client.get("/api/fridge").json()}


def test_warn_days_change_does_not_move_sweep_or_consume(client):
    # 冻饺的在架竞争批只有过期脏种,先到期资格集里只有这笔 5 天后到期的批。
    _, lot_id = stage_and_confirm(client, item_id=3, qty=6, expiry=SOON5)

    set_warn(3)
    exp3 = expected_expired_ids()  # 先算期望再扫: 收走是破坏性操作
    assert set(client.post("/api/expire-sweep").json()["expired_ids"]) == exp3
    assert lot_id not in {a["id"] for a in client.get("/api/alerts").json()}  # 5 天 > 3,不上条

    set_warn(7)
    # 收走不随新天数多扫(旧逻辑会把 5 天后到期的批也扫走)
    exp7 = expected_expired_ids()
    assert set(client.post("/api/expire-sweep").json()["expired_ids"]) == exp7
    assert lot_id in {x["id"] for x in client.get("/api/fridge").json()}  # 还在架上
    a7 = {a["id"]: a for a in client.get("/api/alerts").json()}
    assert a7[lot_id]["level"] == "soon" and a7[lot_id]["days_left"] == 5  # 只有顶条阈值动
    # 先到期资格也不动: 未过期可扣,且只扣到它
    resp = client.post("/api/consume", json={"item_id": 3, "qty": 6})
    assert resp.status_code == 200
    assert {d["lot_id"] for d in resp.json()["deductions"]} == {lot_id}

    set_warn(3)
    exp3b = expected_expired_ids()
    assert set(client.post("/api/expire-sweep").json()["expired_ids"]) == exp3b


def test_confirm_does_not_wash_dirty_seeds(client):
    before = lots_rows()
    stage_and_confirm(client, item_id=1, qty=5, expiry=FUTURE)
    after = lots_rows()

    assert len(after) == len(before) + 1
    before_by_id = {r["id"]: r for r in before}
    for r in after:
        if r["id"] in before_by_id:
            assert r == before_by_id[r["id"]]  # 既有行(脏种/负余量)原样不动
        else:
            assert r["data_quality"] == "clean" and r["qty_remain"] == 5
    assert sum(1 for r in after if r["data_quality"] == "dirty") == 2  # 脏种没被洗白
    assert any(r["qty_remain"] == -3 for r in after)  # 负余量种行原样还在


def test_consume_deducts_real_rows_and_no_ghost_ids(client):
    _, lot_id = stage_and_confirm(client, item_id=1, qty=3, expiry=FUTURE)
    before = {r["id"]: r["qty_remain"] for r in lots_rows()}

    resp = client.post("/api/consume", json={"item_id": 1, "qty": 3})
    assert resp.status_code == 200
    deds = resp.json()["deductions"]
    assert all(d["lot_id"] in before for d in deds)  # 回包里的号都对得上在架主键
    assert all(d["lot_id"] < 900000 for d in deds)
    # 牛奶两笔种批均已过期,资格集里只有刚确认这笔: 扣的正是它
    assert [d["lot_id"] for d in deds] == [lot_id]

    after = {r["id"]: r["qty_remain"] for r in lots_rows()}
    assert abs(sum(before.values()) - sum(after.values()) - 3) < 1e-9  # 余量真扣了
    assert after[lot_id] == 0
    assert lot_id not in {x["id"] for x in client.get("/api/fridge").json()}


def test_consume_non_positive_qty_rejected_without_changes(client):
    before = lots_rows()
    resp = client.post("/api/consume", json={"item_id": 1, "qty": 0})
    assert resp.status_code == 400 and resp.json()["detail"] == "qty_non_positive"
    assert lots_rows() == before
