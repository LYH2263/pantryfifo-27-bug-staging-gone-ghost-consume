"""落层确认对齐契约: 待落 -> 确认 -> 上架, 三处(顶条/收走/FEFO)同一套过期判定。

每条用例独立空库(tmp DATA_DIR, startup 重新播种)。日期一律相对今天生成,
与种子里的固定日期解耦; 日期敏感断言只用 item 3(冻饺), 其种子批 2025-01-01
必然已过期, 不干扰资格判定。
"""
import os
import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import db_path
from app.main import app

TODAY = date.today()


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def set_warn(days: int) -> None:
    c = sqlite3.connect(db_path())
    c.execute("UPDATE settings SET value=? WHERE key='warn_days'", (str(days),))
    c.commit()
    c.close()


def stage(client, item_id, qty, expiry) -> int:
    r = client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def fridge_ids(client, layer=None):
    url = "/api/fridge" + (f"?layer={layer}" if layer else "")
    return [r["id"] for r in client.get(url).json()]


def test_confirm_success_staging_empty_and_shelf_gains_one_real_row(client):
    before_staging = len(client.get("/api/staging").json())
    before_fridge = len(client.get("/api/fridge").json())

    sid = stage(client, 1, 5, d(10))
    assert len(client.get("/api/staging").json()) == before_staging + 1

    r = client.post(f"/api/staging/{sid}/confirm")
    assert r.status_code == 200, r.text
    lot_id = r.json()["lot_id"]

    # 成功: 待落回到点下去之前, 在架恰好多一行, 且总表与层页是同一笔。
    assert len(client.get("/api/staging").json()) == before_staging
    all_rows = client.get("/api/fridge").json()
    assert len(all_rows) == before_fridge + 1
    mine = [x for x in all_rows if x["id"] == lot_id]
    assert len(mine) == 1 and mine[0]["qty_remain"] == 5 and mine[0]["status"] == "on_shelf"
    assert lot_id in fridge_ids(client, "upper")      # 牛奶在上层
    assert lot_id not in fridge_ids(client, "mid")


def test_confirm_failure_adds_no_shelf_row_and_staging_count_stays(client):
    before_staging = len(client.get("/api/staging").json())
    before_fridge = len(client.get("/api/fridge").json())

    # 数量非正: 失败, 待落条数停在失败前, 在架不多行。
    sid = stage(client, 1, 0, d(10))
    r = client.post(f"/api/staging/{sid}/confirm")
    assert r.status_code == 400 and r.json()["detail"] == "qty_non_positive"
    sid_neg = stage(client, 1, -2, d(10))
    assert client.post(f"/api/staging/{sid_neg}/confirm").status_code == 400

    # 暂存不存在/已确认: staging_empty。
    assert client.post("/api/staging/999999/confirm").status_code == 404
    ok_sid = stage(client, 1, 3, d(10))
    assert client.post(f"/api/staging/{ok_sid}/confirm").status_code == 200
    r2 = client.post(f"/api/staging/{ok_sid}/confirm")
    assert r2.status_code == 404 and r2.json()["detail"] == "staging_empty"

    # 品项不存在: 入库即拒, 不进暂存。
    assert client.post("/api/lots", json={"item_id": 999, "qty": 1, "expiry": d(1)}).status_code == 404

    # 两次失败 + 一次成功: 待落净增只有两条失败遗留, 在架只多成功那一行。
    assert len(client.get("/api/staging").json()) == before_staging + 2
    assert len(client.get("/api/fridge").json()) == before_fridge + 1


def test_expired_staged_lot_one_eligibility_across_alerts_sweep_fefo(client):
    client.post("/api/expire-sweep")  # 清掉必然过期的种子批, 只看本例
    sid = stage(client, 3, 2, d(-1))  # 昨天到期
    lot_id = client.post(f"/api/staging/{sid}/confirm").json()["lot_id"]

    # 紧急条: 按过期报出同一 id。
    expired = [a for a in client.get("/api/alerts").json() if a["level"] == "expired"]
    assert lot_id in [a["id"] for a in expired]

    # 先到期: 过期批不可扣, 回包不带这个号。
    r = client.post("/api/consume", json={"item_id": 3, "qty": 1})
    assert r.status_code == 409
    assert lot_id not in [x["lot_id"] for x in r.json()["detail"]["deductions"]]

    # 收走: 名单带这个号, 扫后在架与紧急条都不再出现。
    swept = client.post("/api/expire-sweep").json()["expired_ids"]
    assert lot_id in swept
    assert lot_id not in fridge_ids(client)
    assert lot_id not in [a["id"] for a in client.get("/api/alerts").json()]


def test_warn_days_change_moves_banner_only_not_eligibility(client):
    client.post("/api/expire-sweep")
    sid = stage(client, 3, 4, d(5))  # 5 天后到期: 3 天不预警, 7 天预警
    lot_id = client.post(f"/api/staging/{sid}/confirm").json()["lot_id"]

    def soon_ids():
        return [a["id"] for a in client.get("/api/alerts").json() if a["level"] == "soon"]

    set_warn(3)
    assert lot_id not in soon_ids()
    set_warn(7)
    assert lot_id in soon_ids()
    set_warn(3)  # 改回去, 资格判定两边都不动

    for w in (3, 7):
        set_warn(w)
        # 收走只认 expiry < today, 不认预警天数。
        assert lot_id not in client.post("/api/expire-sweep").json()["expired_ids"]
        # 先到期只认未过期, 也不认预警天数。
        r = client.post("/api/consume", json={"item_id": 3, "qty": 1})
        assert r.status_code == 200
        assert [x["lot_id"] for x in r.json()["deductions"]] == [lot_id]


def test_confirm_does_not_wash_dirty_seed_rows(client):
    dirty_before = {
        r["id"]: (r["qty_remain"], r["data_quality"])
        for r in client.get("/api/fridge").json()
        if r["data_quality"] == "dirty"
    }
    assert dirty_before, "种子应带脏批"

    sid = stage(client, 2, 4, d(30))  # 与脏种同品(鸡蛋)
    assert client.post(f"/api/staging/{sid}/confirm").status_code == 200

    dirty_after = {
        r["id"]: (r["qty_remain"], r["data_quality"])
        for r in client.get("/api/fridge").json()
        if r["data_quality"] == "dirty"
    }
    assert dirty_after == dirty_before  # 脏种原样, 不被这次确认洗成干净


def test_confirm_consume_sweep_stacked_on_same_item_stays_consistent(client):
    client.post("/api/expire-sweep")
    sid = stage(client, 3, 6, d(1))  # 明天到期
    lot_id = client.post(f"/api/staging/{sid}/confirm").json()["lot_id"]

    # 扣减落在真实行上: 余量真的少了。
    r = client.post("/api/consume", json={"item_id": 3, "qty": 2})
    assert r.status_code == 200 and [x["lot_id"] for x in r.json()["deductions"]] == [lot_id]
    mine = [x for x in client.get("/api/fridge").json() if x["id"] == lot_id]
    assert len(mine) == 1 and mine[0]["qty_remain"] == 4

    # 紧急条按真实在架报临期(不再把确认过的批当未上架滤掉)。
    soon = [a for a in client.get("/api/alerts").json() if a["level"] == "soon"]
    assert lot_id in [a["id"] for a in soon]

    # 收走不动未过期批; 总表与层页仍是同一笔。
    assert lot_id not in client.post("/api/expire-sweep").json()["expired_ids"]
    assert fridge_ids(client, "lower").count(lot_id) == 1
    assert fridge_ids(client).count(lot_id) == 1
