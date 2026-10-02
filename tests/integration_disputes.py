"""播放争议暂缓：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_disputes.py

全部场景共享同一期间（2026-02），按作品/链隔离金额，避免数字纠缠：
- 作品 W-D1（DA 60% / DB 40%）：开启/幂等/冲突/同链重复开启/出单撤销/
  更正继承/解除恢复/解除后重开/旧争议号不重开/无变化不出新单
- 作品 W-D2（无协议）：争议优先于缺协议；解除后回到缺协议待处理
- 作品 W-D3（DC 100%）：并发同号开启恰一笔、同链并发开启恰一条、
  争议与出单并发一致不重复出单
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_dsp_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    period = {"start": "2026-02-01T00:00:00+08:00",
              "end": "2026-03-01T00:00:00+08:00"}

    def setup_work(code, shares):
        """登记作品 + 权利人和协议（shares: {holder: percent-int}）。"""
        c.post("/works", json={"code": code, "title": code})
        for h in shares:
            c.post("/rightsholders", json={"code": h, "name": h})
        c.post("/agreements", json={
            "work_code": code,
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": h, "percent": str(p)}
                       for h, p in shares.items()]})

    def import_play(import_id, work, amount, corrects=None):
        r = c.post("/plays/import", json={
            "import_id": import_id, "work_code": work,
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": amount,
            "corrects_id": corrects})
        assert r.status_code in (200, 201), r.text
        return r.json()

    def issue():
        r = c.post("/statements/issue", json=period)
        assert r.status_code in (200, 201), r.text
        return r.json()

    def preview():
        r = c.get("/statements/preview", params=period)
        assert r.status_code == 200, r.text
        return r.json()

    def payable_map(payload):
        return {l["holder_code"]: l["amount"] for l in payload["payable"]}

    def delta_map(payload):
        return {l["holder_code"]: l["delta"] for l in payload["payable"]}

    def pending_map(payload):
        return {p["import_id"]: p["reason"] for p in payload["pending"]}

    def open_dispute(import_id, ref, reason):
        return c.post("/plays/disputes", json={
            "import_id": import_id, "dispute_ref": ref, "reason": reason})

    def release(ref):
        return c.post(f"/plays/disputes/{ref}/release")

    def get_dispute(ref):
        return c.get(f"/plays/disputes/{ref}")

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # =================================================================
        # 一、W-D1 / DA 60% / DB 40%：开启、幂等、冲突、出单撤销、
        #     更正继承、解除恢复、解除后重开
        # =================================================================
        setup_work("W-D1", {"DA": 60, "DB": 40})
        import_play("P-D1", "W-D1", 1000)
        import_play("P-D1-R1", "W-D1", 800, corrects="P-D1")
        first = issue()
        check("initial billed DA/DB 480/320",
              payable_map(first) == {"DA": 480, "DB": 320}, str(first["payable"]))
        initial_batch = first["batch_id"]

        # ---- 凭链内任一编号（此处用链尾）开启争议 ----
        r = open_dispute("P-D1-R1", "DSP-1", "计费依据存疑：金额与平台账单不符")
        check("open dispute 201", r.status_code == 201, r.text)
        body = r.json()
        check("open dispute payload",
              body["status"] == "created"
              and body["dispute_status"] == "open"
              and body["root_import_id"] == "P-D1"
              and body["open_import_id"] == "P-D1-R1"
              and body["tail_import_id"] == "P-D1-R1"
              and body["released_at"] is None
              and [e["action"] for e in body["events"]] == ["open"]
              and body["events"][0]["note"] == "计费依据存疑：金额与平台账单不符",
              str(body))
        dispute_id = body["dispute_id"]

        # 查询：原始编号、当前链尾、状态与操作记录
        got = get_dispute("DSP-1").json()
        check("get dispute shows root/tail/status/events",
              got["status"] == "open"
              and got["dispute_status"] == "open"
              and got["root_import_id"] == "P-D1"
              and got["tail_import_id"] == "P-D1-R1"
              and len(got["events"]) == 1, str(got))

        # ---- 同号同内容重试 => 200 duplicate 返回原记录 ----
        r = open_dispute("P-D1-R1", "DSP-1", "计费依据存疑：金额与平台账单不符")
        check("same ref same content duplicate",
              r.status_code == 200
              and r.json()["status"] == "duplicate"
              and r.json()["dispute_id"] == dispute_id, r.text)

        # ---- 同号异内容（原因不同 / 凭据编号不同）=> 409，状态不变 ----
        r = open_dispute("P-D1-R1", "DSP-1", "另一个原因")
        check("same ref different reason 409",
              r.status_code == 409
              and r.json()["error"] == "dispute_ref_conflict", r.text)
        r = open_dispute("P-D1", "DSP-1", "计费依据存疑：金额与平台账单不符")
        check("same ref different import_id 409",
              r.status_code == 409
              and r.json()["error"] == "dispute_ref_conflict", r.text)
        check("conflict leaves dispute open",
              get_dispute("DSP-1").json()["dispute_status"] == "open")

        # ---- 未知播放 => 404，不留争议 ----
        r = open_dispute("NO-SUCH-PLAY", "DSP-X", "原因")
        check("unknown play 404",
              r.status_code == 404 and r.json()["error"] == "play_not_found",
              r.text)
        check("unknown play leaves nothing",
              get_dispute("DSP-X").status_code == 404)

        # ---- 同链重复开启未解除争议（凭链根编号同样命中）=> 409 ----
        r = open_dispute("P-D1", "DSP-2", "另一财务再次登记")
        check("same chain second open 409",
              r.status_code == 409
              and r.json()["error"] == "dispute_already_open"
              and r.json()["detail"]["existing_dispute_ref"] == "DSP-1",
              r.text)
        check("rejected second open leaves nothing",
              get_dispute("DSP-2").status_code == 404)

        # ---- 未解除期间：试算列争议待处理、不计应付 ----
        pv = preview()
        check("preview pending lists tail as disputed",
              pending_map(pv).get("P-D1-R1", "").startswith(
                  "billing disputed; correction chain on hold (dispute DSP-1)"),
              str(pv["pending"]))
        check("preview payable zeroes disputed chain",
              payable_map(pv) == {"DA": 0, "DB": 0}, str(pv["payable"]))

        # ---- 再次出单：按追补规则撤销该链已计金额（负额），旧单冻结 ----
        sup1 = issue()
        check("supplement revokes billed amounts",
              sup1["kind"] == "supplement"
              and delta_map(sup1) == {"DA": -480, "DB": -320},
              str(sup1["payable"]))
        check("supplement pending snapshot lists disputed tail",
              "dispute DSP-1" in pending_map(sup1).get("P-D1-R1", ""),
              str(sup1["pending"]))
        attr = c.get(f"/statements/{sup1['batch_id']}/attributions").json()
        removed = [r for r in attr["records"] if r["change_type"] == "removed"]
        check("supplement evidence removed rows",
              {(r["holder_code"], r["amount"]) for r in removed}
              == {("DA", -480), ("DB", -320)}
              and all(r["root_import_id"] == "P-D1" for r in removed),
              str(attr["records"]))
        check("supplement reconciles",
              all(t["reconciles"] for t in attr["holder_totals"]))
        # 已出具单据及归因保持冻结
        attr0 = c.get(f"/statements/{initial_batch}/attributions").json()
        check("initial statement evidence frozen",
              {(r["holder_code"], r["amount"]) for r in attr0["records"]}
              == {("DA", 480), ("DB", 320)}
              and all(t["reconciles"] for t in attr0["holder_totals"]),
              str(attr0["records"]))

        # ---- 暂缓期间更正：后续更正自动继承争议，链尾前移 ----
        import_play("P-D1-R2", "W-D1", 500, corrects="P-D1-R1")
        got = get_dispute("DSP-1").json()
        check("correction inherits dispute; tail moves",
              got["tail_import_id"] == "P-D1-R2"
              and got["dispute_status"] == "open", str(got))
        pv = preview()
        check("preview pending shows new tail, old tail gone",
              "P-D1-R2" in pending_map(pv)
              and "P-D1-R1" not in pending_map(pv), str(pv["pending"]))
        # 应付无变化（仍挂起为 0）=> 不产生新单
        sup_noop = issue()
        check("no payable change while held => no new batch",
              sup_noop["batch_id"] == sup1["batch_id"]
              and sup_noop["note"] is not None, str(sup_noop))

        # ---- 解除：幂等；恢复按最新链尾与适用协议核算 ----
        r = release("DSP-1")
        check("release 200",
              r.status_code == 200
              and r.json()["status"] == "released"
              and r.json()["dispute_status"] == "released"
              and r.json()["released_at"] is not None
              and [e["action"] for e in r.json()["events"]]
              == ["open", "release"], r.text)
        r = release("DSP-1")
        check("repeat release idempotent",
              r.status_code == 200
              and r.json()["status"] == "duplicate"
              and len(r.json()["events"]) == 2, r.text)
        r = release("DSP-UNKNOWN")
        check("release unknown 404",
              r.status_code == 404 and r.json()["error"] == "dispute_not_found",
              r.text)
        check("get unknown dispute 404",
              get_dispute("DSP-UNKNOWN").status_code == 404)

        sup2 = issue()
        check("after release supplement restores latest tail 500 => 300/200",
              sup2["kind"] == "supplement"
              and delta_map(sup2) == {"DA": 300, "DB": 200},
              str(sup2["payable"]))
        attr = c.get(f"/statements/{sup2['batch_id']}/attributions").json()
        added = [r for r in attr["records"] if r["change_type"] == "added"]
        check("restore evidence added rows on new tail",
              {(r["holder_code"], r["amount"]) for r in added}
              == {("DA", 300), ("DB", 200)}
              and all(r["tail_import_id"] == "P-D1-R2" for r in added),
              str(attr["records"]))

        # ---- 解除后可用新号重开（凭链中间编号同样定位整条链）----
        r = open_dispute("P-D1-R1", "DSP-3", "新一轮核查")
        check("reopen with new ref after release (via middle record)",
              r.status_code == 201
              and r.json()["status"] == "created"
              and r.json()["root_import_id"] == "P-D1"
              and r.json()["tail_import_id"] == "P-D1-R2", r.text)
        # 旧争议号同内容重试只返回原已解除记录，不会重开
        r = open_dispute("P-D1-R1", "DSP-1", "计费依据存疑：金额与平台账单不符")
        check("old ref retry returns released record, does not reopen",
              r.status_code == 200
              and r.json()["status"] == "duplicate"
              and r.json()["dispute_status"] == "released", r.text)
        check("chain held again by new dispute",
              "P-D1-R2" in pending_map(preview()))
        release("DSP-3")
        # 开启又解除、期间未出单：应付无变化 => 不产生新单
        sup3 = issue()
        check("open+release without intervening issue => no new batch",
              sup3["batch_id"] == sup2["batch_id"]
              and sup3["note"] is not None, str(sup3))

        # =================================================================
        # 二、W-D2（无协议）：争议优先于缺协议；解除后回到缺协议待处理
        # =================================================================
        c.post("/works", json={"code": "W-D2", "title": "无协议作品"})
        import_play("P-D2", "W-D2", 700)
        pv = preview()
        check("no agreement pending reason",
              pending_map(pv).get("P-D2")
              == "no matching agreement at play occurrence time",
              str(pv["pending"]))

        r = open_dispute("P-D2", "DSP-NA", "无协议且金额存疑")
        check("open dispute on agreement-less chain",
              r.status_code == 201, r.text)
        pv = preview()
        check("dispute takes precedence over missing agreement",
              pending_map(pv).get("P-D2", "").startswith("billing disputed"),
              str(pv["pending"]))
        iss = issue()
        check("issue pending snapshot lists disputed (no payable change)",
              iss["batch_id"] == sup3["batch_id"]
              and "dispute DSP-NA" in pending_map(iss).get("P-D2", ""),
              str(iss))

        release("DSP-NA")
        pv = preview()
        check("after release back to missing-agreement pending",
              pending_map(pv).get("P-D2")
              == "no matching agreement at play occurrence time",
              str(pv["pending"]))
        # 应付无变化（仍待处理、无应付）=> 不产生新单
        iss2 = issue()
        check("no payable change after release => no new batch",
              iss2["batch_id"] == iss["batch_id"]
              and iss2["note"] is not None, str(iss2))

        # =================================================================
        # 三、W-D3 / DC 100%：并发
        # =================================================================
        setup_work("W-D3", {"DC": 100})
        import_play("P-D3", "W-D3", 100)
        issue()  # DC 100 入账

        # 1) 同争议号同内容 8 并发开启 => 恰 1 笔 created，7 笔 duplicate
        results = []

        def fire_open_same():
            rr = open_dispute("P-D3", "DSP-C1", "并发同号")
            results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_open_same) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        created = sum(1 for _, x in results if x["status"] == "created")
        dup = sum(1 for _, x in results if x["status"] == "duplicate")
        ids = {x["dispute_id"] for _, x in results}
        check("concurrent same-ref open exactly one created",
              created == 1 and dup == 7 and len(ids) == 1, str(results))
        release("DSP-C1")

        # 2) 同链不同争议号 8 并发开启 => 恰 1 条 201，其余 409
        results2 = []

        def fire_open_new(i):
            rr = open_dispute("P-D3", f"DSP-C2-{i}", "并发异号")
            results2.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_open_new, args=(i,))
              for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        n_created = sum(1 for s, _ in results2 if s == 201)
        n_conflict = sum(
            1 for s, x in results2
            if s == 409 and x["error"] == "dispute_already_open")
        check("concurrent same-chain opens exactly one wins",
              n_created == 1 and n_conflict == 7, str(results2))
        for s, x in results2:
            if s == 201:
                release(x["dispute_ref"])

        # 3) 争议开启与出单并发：各自基于一致已提交状态，
        #    不重复出单、不漏算；最终态必然收敛到"争议挂起"
        import_play("P-D3B", "W-D3", 40)
        issue()  # DC 140 入账
        outcomes = {"issue_ok": 0, "errors": []}
        barrier = threading.Barrier(6)

        def fire_issue():
            barrier.wait()
            try:
                rr = c.post("/statements/issue", json=period)
                if rr.status_code == 200:
                    outcomes["issue_ok"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        def fire_open():
            barrier.wait()
            try:
                rr = open_dispute("P-D3B", "DSP-C3", "与出单并发")
                if rr.status_code not in (200, 201):
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        threads = [threading.Thread(target=fire_issue) for _ in range(5)]
        threads.append(threading.Thread(target=fire_open))
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("concurrent issue+open no errors",
              not outcomes["errors"] and outcomes["issue_ok"] == 5,
              str(outcomes))
        # 最终再出具一次，状态必然收敛：P-D3B 挂起、仅 P-D3 计 100
        final = issue()
        check("final state converges to disputed hold",
              payable_map(final) == {"DA": 300, "DB": 200, "DC": 100}
              and "dispute DSP-C3" in pending_map(final).get("P-D3B", ""),
              str(final))
        # 该期间恰一张初次单，绝无重复出单
        batches = c.get("/statements", params=period).json()
        check("exactly one initial batch for period",
              sum(1 for b in batches if b["kind"] == "initial") == 1,
              str([b["kind"] for b in batches]))
        # 解除后恢复：P-D3B 40 分补回，DC 合计 140
        release("DSP-C3")
        restored = issue()
        check("after release payable restored",
              payable_map(restored) == {"DA": 300, "DB": 200, "DC": 140},
              str(restored["payable"]))
        led = c.get("/rightsholders/DC/ledger").json()
        check("ledger billed_total consistent",
              led["billed_total"] == 140, str(led))

        # =================================================================
        # 四、校验类：字段非法 422；争议与操作记录不可改写（触发器）
        # =================================================================
        r = c.post("/plays/disputes", json={
            "import_id": "P-D3", "dispute_ref": "", "reason": "x"})
        check("empty dispute_ref 422", r.status_code == 422, r.text)
        r = c.post("/plays/disputes", json={
            "import_id": "P-D3", "dispute_ref": "DSP-BAD", "reason": ""})
        check("empty reason 422", r.status_code == 422, r.text)

        # 新开一条未解除争议用于触发器校验
        r = open_dispute("P-D3", "DSP-T", "触发器校验")
        assert r.status_code == 201, r.text
        with db.pool.connection() as raw:
            denied = []
            for stmt in (
                # 内容字段不可改
                "UPDATE play_disputes SET reason = 'x' "
                "WHERE dispute_ref = 'DSP-T'",
                "UPDATE play_disputes SET root_import_id = 'X' "
                "WHERE dispute_ref = 'DSP-T'",
                # 已解除争议不可再迁移
                "UPDATE play_disputes SET status = 'open' "
                "WHERE dispute_ref = 'DSP-C1'",
                # 任何删除拒绝
                "DELETE FROM play_disputes WHERE dispute_ref = 'DSP-T'",
                # 操作记录不可改/删
                "UPDATE play_dispute_events SET action = 'release'",
                "DELETE FROM play_dispute_events",
            ):
                try:
                    raw.execute(stmt)
                    denied.append(False)
                except psycopg.Error:
                    raw.rollback()
                    denied.append(True)
        check("dispute and events immutable by triggers",
              all(denied), str(denied))
        check("dispute still open after tamper attempts",
              get_dispute("DSP-T").json()["dispute_status"] == "open")
        release("DSP-T")

    print()
    if failures:
        print("FAILED:", len(failures), "->", failures)
        raise SystemExit(1)
    print("ALL DISPUTE INTEGRATION TESTS PASSED")
finally:
    server.cleanup()
