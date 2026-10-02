"""播放更正链计费争议（暂缓/解除）端到端集成测试
（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_disputes.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_disp_pg_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def agree(c, work, frm, to, shares):
        r = c.post("/agreements", json={
            "work_code": work, "valid_from": frm, "valid_to": to,
            "shares": [{"holder_code": h, "percent": p} for h, p in shares]})
        assert r.status_code == 201, r.text

    def open_dispute(c, play_id, ref, reason, code=201):
        r = c.post("/play-disputes", json={
            "play_import_id": play_id, "dispute_ref": ref, "reason": reason})
        assert r.status_code == code, r.text
        return r

    FEB = {"start": "2026-02-01T00:00:00+08:00",
           "end": "2026-03-01T00:00:00+08:00"}

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        c.post("/works", json={"code": "W1", "title": "作品一"})
        c.post("/works", json={"code": "W2", "title": "作品二"})
        agree(c, "W1", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "62.5"), ("B", "30"), ("C", "7.5")])
        # W2 不建协议：用于"缺协议"场景

        # 导入链 P1(100) -> P1R1(80)，与无协议链 Q1(100)
        for pid, amount, corr in (
            ("P1", 100, None),
            ("P1R1", 80, "P1"),
            ("Q1", 100, None),
        ):
            r = c.post("/plays/import", json={
                "import_id": pid, "work_code": "W1" if pid != "Q1" else "W2",
                "occurred_at": "2026-02-10T20:00:00+08:00",
                "amount": amount, "corrects_id": corr})
            check(f"import {pid}", r.status_code == 201, r.text)

        # ---------- 1) 凭链内编号（链尾 P1R1）登记暂缓 ----------
        r = open_dispute(c, "P1R1", "DSP-1", "播放量来源存疑")
        b = r.json()
        check("open 201 created", r.status_code == 201 and b["status"] == "created")
        check("open resolved root", b["root_import_id"] == "P1")
        check("open anchor kept", b["anchor_import_id"] == "P1R1")
        check("open current tail", b["current_tail_import_id"] == "P1R1")
        check("open status", b["dispute_status"] == "open" and b["released_at"] is None)
        check("open operations",
              [(o["operation"], o["play_import_id"]) for o in b["operations"]]
              == [("opened", "P1R1")], str(b["operations"]))

        # ---------- 2) 同号同内容重试：原结果，状态不变，不新增操作 ----------
        r = open_dispute(c, "P1R1", "DSP-1", "播放量来源存疑", code=200)
        b2 = r.json()
        check("retry duplicate", b2["status"] == "duplicate"
              and b2["dispute_status"] == "open"
              and b2["opened_at"] == b["opened_at"])
        check("retry no new op", len(b2["operations"]) == 1)

        # 同号用链内另一编号（解析到同一链）也视为同内容（链相同）
        r = open_dispute(c, "P1", "DSP-1", "播放量来源存疑", code=200)
        check("retry via other chain id same chain",
              r.json()["status"] == "duplicate")

        # ---------- 3) 异内容复用：异原因 409；异链（Q1）409 ----------
        r = c.post("/play-disputes", json={
            "play_import_id": "P1R1", "dispute_ref": "DSP-1", "reason": "另一个原因"})
        check("diff reason 409", r.status_code == 409
              and r.json()["error"] == "dispute_ref_conflict", r.text)
        r = c.post("/play-disputes", json={
            "play_import_id": "Q1", "dispute_ref": "DSP-1", "reason": "播放量来源存疑"})
        check("diff chain 409", r.status_code == 409
              and r.json()["error"] == "dispute_ref_conflict", r.text)

        # ---------- 4) 未知播放 404；同链重复开启（新争议号）409 且状态不变 ----------
        r = c.post("/play-disputes", json={
            "play_import_id": "GHOST", "dispute_ref": "DSP-X", "reason": "x"})
        check("unknown play 404", r.status_code == 404
              and r.json()["error"] == "play_not_found", r.text)
        r = c.post("/play-disputes", json={
            "play_import_id": "P1", "dispute_ref": "DSP-2", "reason": "又一条争议"})
        check("already open 409", r.status_code == 409
              and r.json()["error"] == "dispute_already_open", r.text)
        # DSP-2 未创建
        check("rejected ref absent",
              c.get("/play-disputes/DSP-2").status_code == 404)
        # 原争议状态不变
        check("state unchanged after reject",
              c.get("/play-disputes/DSP-1").json()["dispute_status"] == "open")

        # 字段校验
        r = c.post("/play-disputes", json={
            "play_import_id": "P1", "dispute_ref": "", "reason": "x"})
        check("empty ref 422", r.status_code == 422)

        # ---------- 5) 暂缓期间试算：争议链列 disputed 不计应付；缺协议为先 ----------
        r = c.get("/statements/preview", params=FEB)
        pv = r.json()
        pending_by = {p["import_id"]: p for p in pv["pending"]}
        check("preview disputed tail", pending_by["P1R1"]["reason_code"] == "disputed"
              and pending_by["P1R1"]["dispute_ref"] == "DSP-1")
        check("preview no-agreement still", pending_by["Q1"]["reason_code"] == "no_agreement"
              and pending_by["Q1"]["dispute_ref"] is None)
        check("preview payable empty", pv["payable"] == [], str(pv["payable"]))

        # ---------- 6) 暂缓期间出单：初次单无应付，两条待处理 ----------
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        check("issue initial under dispute",
              body["kind"] == "initial" and body["payable"] == [], body)
        check("issue pending 2",
              {p["import_id"]: p["reason_code"] for p in body["pending"]}
              == {"P1R1": "disputed", "Q1": "no_agreement"}, str(body["pending"]))

        # ---------- 7) 暂缓后新增更正自动继承：链尾前移到 P1R2 ----------
        r = c.post("/plays/import", json={
            "import_id": "P1R2", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 30,
            "corrects_id": "P1R1"})
        check("correction during dispute", r.status_code == 201, r.text)
        b = c.get("/play-disputes/DSP-1").json()
        check("query shows new tail", b["current_tail_import_id"] == "P1R2")
        r = c.get("/statements/preview", params=FEB)
        pending_by = {p["import_id"]: p for p in r.json()["pending"]}
        check("new tail inherits dispute", "P1R2" in pending_by
              and pending_by["P1R2"]["reason_code"] == "disputed")
        check("old tail gone from pending", "P1R1" not in pending_by)

        # 争议仍在时再次出单：应付无变化，不出新单
        r = c.post("/statements/issue", json=FEB)
        check("no new batch while disputed",
              r.json()["kind"] == "initial", r.text)
        check("one batch only", len(c.get("/statements", params=FEB).json()) == 1)

        # ---------- 8) 解除：幂等；未知争议号 404 ----------
        r = c.post("/play-disputes/NOPE/release")
        check("release unknown 404", r.status_code == 404
              and r.json()["error"] == "play_dispute_not_found", r.text)

        r = c.post("/play-disputes/DSP-1/release")
        b = r.json()
        check("release 200", r.status_code == 200 and b["status"] == "released"
              and b["dispute_status"] == "released" and b["released_at"] is not None)
        check("release ops",
              [o["operation"] for o in b["operations"]] == ["opened", "released"])
        check("release event refs same play",
              b["operations"][1]["play_import_id"] == "P1R1")

        r = c.post("/play-disputes/DSP-1/release")
        b2 = r.json()
        check("release idempotent", r.status_code == 200 and b2["status"] == "duplicate"
              and b2["dispute_status"] == "released")
        check("release idempotent no extra op", len(b2["operations"]) == 2)

        # 解除后再试算：按最新链尾 P1R2(30) 与协议恢复 19/9/2；Q1 仍缺协议
        r = c.get("/statements/preview", params=FEB)
        pv = r.json()
        got = {x["holder_code"]: x["amount"] for x in pv["payable"]}
        check("recovered preview 19/9/2", got == {"A": 19, "B": 9, "C": 2}, str(got))
        check("Q1 stays no-agreement",
              [p["reason_code"] for p in pv["pending"]] == ["no_agreement"])

        # ---------- 9) 解除后重开：旧号同内容仍幂等（不复活），新号可开 ----------
        r = c.post("/play-disputes", json={
            "play_import_id": "P1R2", "dispute_ref": "DSP-1", "reason": "播放量来源存疑"})
        check("old ref stays idempotent released",
              r.status_code == 200 and r.json()["dispute_status"] == "released")
        r = open_dispute(c, "P1R2", "DSP-3", "复核后再次暂缓")
        check("reopen with new ref",
              r.status_code == 201 and r.json()["dispute_status"] == "open")
        c.post("/play-disputes/DSP-3/release")

        # ---------- 10) 解除后再出单：追补单入账（已出单据/归因冻结） ----------
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        got = {x["holder_code"]: (x["amount"], x["delta"]) for x in body["payable"]}
        check("supplement after release",
              body["kind"] == "supplement"
              and got == {"A": (19, 19), "B": (9, 9), "C": (2, 2)}, str(got))
        check("only Q1 pending",
              [(p["import_id"], p["reason_code"]) for p in body["pending"]]
              == [("Q1", "no_agreement")], str(body["pending"]))

        # 归因：初次单无证据（争议不计应付），追补单按当前链尾 added
        batches = c.get("/statements", params=FEB).json()
        initial_id, supp_id = batches[0]["batch_id"], batches[1]["batch_id"]
        attr0 = c.get(f"/statements/{initial_id}/attributions").json()
        attr1 = c.get(f"/statements/{supp_id}/attributions").json()
        check("initial no evidence", attr0["total_records"] == 0
              and attr0["holder_totals"] == [])
        roots = {rec["root_import_id"] for rec in attr1["records"]}
        check("supplement evidence root P1", roots == {"P1"}, str(roots))
        check("supplement evidence tail P1R2",
              {rec["tail_import_id"] for rec in attr1["records"]} == {"P1R2"})

        # 应付无变化再出单：不产生新单
        r = c.post("/statements/issue", json=FEB)
        check("no new batch unchanged",
              r.json()["batch_id"] == supp_id and r.json()["kind"] == "supplement")
        check("still two batches",
              len(c.get("/statements", params=FEB).json()) == 2)

        # ---------- 11) 争议优先于缺协议：Q1 暂缓后即使后补协议也不计应付 ----------
        open_dispute(c, "Q1", "DSP-Q", "无协议链也要先挂争议")
        # 补 W2 协议（含 2 月）
        agree(c, "W2", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "100")])
        r = c.get("/statements/preview", params=FEB)
        pending_by = {p["import_id"]: p for p in r.json()["pending"]}
        check("dispute beats agreement",
              pending_by.get("Q1", {}).get("reason_code") == "disputed",
              str(pending_by))
        # 解除后 Q1 按后补协议恢复
        c.post("/play-disputes/DSP-Q/release")
        r = c.get("/statements/preview", params=FEB)
        pending_by = {p["import_id"]: p for p in r.json()["pending"]}
        check("Q1 recovered after release+agreement", "Q1" not in pending_by,
              str(pending_by))

        # ---------- 12) 已入账后再暂缓：已出单据冻结，再出单按追补撤销该链 ----------
        # 先就 P1 链当前状态（19/9/2）再出单无变化；Q1(100,A100%) 入账
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        check("Q1 supplement billed",
              body["kind"] == "supplement"
              and {x["holder_code"]: x["delta"] for x in body["payable"]
                   if x["delta"] is not None} == {"A": 100}, str(body))
        billed_batch = body["batch_id"]

        # Q1 出单后开争议
        open_dispute(c, "Q1", "DSP-Q2", "出单后发现依据存疑")
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        check("revoke supplement created", body["kind"] == "supplement")
        got = {x["holder_code"]: (x["amount"], x["delta"]) for x in body["payable"]}
        # Q1 撤销 -100：A 当前应付回到 19（P1链），delta -100
        check("revoke delta -100", got.get("A") == (19, -100)
              and all(v[1] is None for k, v in got.items() if k != "A"), str(got))
        check("Q1 disputed pending again",
              [(p["import_id"], p["reason_code"], p["dispute_ref"])
               for p in body["pending"]]
              == [("Q1", "disputed", "DSP-Q2")], str(body["pending"]))
        # 上一张单据与归因未被改写
        prior = c.get(f"/statements/{billed_batch}/attributions").json()
        check("prior evidence frozen",
              any(rec["root_import_id"] == "Q1" and rec["amount"] == 100
                  for rec in prior["records"]), "frozen")
        # 撤销单证据：removed(-100)，且不产生 added（争议中）
        rev = c.get(f"/statements/{body['batch_id']}/attributions").json()
        q1recs = [rec for rec in rev["records"] if rec["root_import_id"] == "Q1"]
        check("revoke removed only",
              len(q1recs) == 1 and q1recs[0]["change_type"] == "removed"
              and q1recs[0]["amount"] == -100, str(q1recs))

        # 解除 Q1：再次出单按最新链尾恢复 +100（A 19 -> 119）
        c.post("/play-disputes/DSP-Q2/release")
        r = c.post("/statements/issue", json=FEB)
        body = r.json()
        check("restore supplement",
              body["kind"] == "supplement"
              and {x["holder_code"]: x["delta"] for x in body["payable"]
                   if x["delta"] is not None} == {"A": 100}, str(body))

        # ---------- 13) 争议操作与出单并发：基于一致已提交状态，不重复出单/漏算 ----------
        c.post("/works", json={"code": "W3", "title": "并发作品"})
        agree(c, "W3", "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00",
              [("A", "100")])
        # 覆盖并发场景所用的 2031 年期间
        agree(c, "W3", "2030-01-01T00:00:00+08:00", "2033-01-01T00:00:00+08:00",
              [("A", "100")])
        c.post("/plays/import", json={
            "import_id": "CC1", "work_code": "W3",
            "occurred_at": "2026-02-15T20:00:00+08:00", "amount": 100})
        # CC2 落在 2031 年独立期间，隔离前述数据
        c.post("/plays/import", json={
            "import_id": "CC2", "work_code": "W3",
            "occurred_at": "2031-02-15T20:00:00+08:00", "amount": 100})
        CC2_PERIOD = {"start": "2031-02-01T00:00:00+08:00",
                      "end": "2031-03-01T00:00:00+08:00"}

        def issue(p):
            try:
                r = c.post("/statements/issue", json=p)
                issue_results.append(r.status_code)
            except Exception as e:  # noqa
                issue_results.append(repr(e))

        # 13a) 出单与"暂缓"并发多次：最终状态自洽——
        #      争议打开后，该期间待处理必含 CC2 且应付不含 CC2；单据数有限（只可能初次/追补）
        issue_results = []
        threads = [
            threading.Thread(target=issue, args=(CC2_PERIOD,)),
            threading.Thread(target=lambda: open_dispute(c, "CC2", "DSP-CC", "并发暂缓")),
            threading.Thread(target=issue, args=(CC2_PERIOD,)),
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("concurrent issue no errors",
              all(x == 200 for x in issue_results), str(issue_results))
        # 争议必已开启成功
        check("concurrent dispute open",
              c.get("/play-disputes/DSP-CC").json()["dispute_status"] == "open")
        # 最新待处理快照必含争议链尾；payable（试算）必无 CC2
        pv = c.get("/statements/preview", params=CC2_PERIOD).json()
        check("concurrent tail disputed in preview",
              any(p["import_id"] == "CC2" and p["reason_code"] == "disputed"
                  for p in pv["pending"]), str(pv["pending"]))
        check("concurrent no payable for disputed",
              all(x["amount"] == 0 for x in pv["payable"]) or pv["payable"] == [],
              str(pv["payable"]))
        # 再出单直至收敛：争议仍在，链尾必挂争议待处理且不计应付；
        # 无论并发窗口中的提交次序如何（先开争议或先出单），最终状态唯一，
        # 且收敛后继续出单不再产生新单（不重复出单/不漏算）。
        def settle(p, max_rounds=5):
            last_n = None
            for _ in range(max_rounds):
                c.post("/statements/issue", json=p)
                n = len(c.get("/statements", params=p).json())
                if n == last_n:
                    return n
                last_n = n
            return last_n

        settled_n = settle(CC2_PERIOD)
        pv = c.get("/statements/preview", params=CC2_PERIOD).json()
        check("concurrent settled tail disputed",
              any(p2["import_id"] == "CC2" and p2["reason_code"] == "disputed"
                  for p2 in pv["pending"]), str(pv["pending"]))
        check("concurrent settled no payable",
              all(x["amount"] == 0 for x in pv["payable"]), str(pv["payable"]))
        r = c.post("/statements/issue", json=CC2_PERIOD)
        check("concurrent converge no dup batch",
              len(c.get("/statements", params=CC2_PERIOD).json()) == settled_n,
              f"settled={settled_n}")

        # 13b) 同争议号并发开启：恰一笔 created，其余幂等/冲突，状态一致
        codes = []

        def fire_open(ref, play, reason):
            r = c.post("/play-disputes", json={
                "play_import_id": play, "dispute_ref": ref, "reason": reason})
            codes.append(r.status_code)

        c.post("/plays/import", json={
            "import_id": "CC3", "work_code": "W3",
            "occurred_at": "2031-02-16T20:00:00+08:00", "amount": 100})
        ts = [threading.Thread(target=fire_open, args=("DSP-CC3", "CC3", "r"))
              for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("same ref concurrent: one 201 rest 200",
              codes.count(201) == 1 and codes.count(200) == 4, str(codes))

        # 13c) 同链不同争议号并发开启：恰一条 open，其余 409
        c.post("/plays/import", json={
            "import_id": "CC4", "work_code": "W3",
            "occurred_at": "2031-02-17T20:00:00+08:00", "amount": 100})
        statuses = {}
        barrier = threading.Barrier(6)

        def fire_open2(i):
            barrier.wait()
            r = c.post("/play-disputes", json={
                "play_import_id": "CC4", "dispute_ref": f"DSP-CC4-{i}", "reason": "r"})
            statuses[i] = r.status_code

        ts = [threading.Thread(target=fire_open2, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        vals = list(statuses.values())
        check("same chain concurrent: one open rest 409",
              vals.count(201) == 1 and vals.count(409) == 5, str(vals))
        open_n = len(c.get("/play-disputes/DSP-CC4-0").json().get("operations", []))
        # 只允许一条成功（编号 0 是否成功取决于调度；统计 open 争议总数）
        r = c.get("/statements/preview", params={
            "start": "2031-02-01T00:00:00+08:00", "end": "2031-03-01T00:00:00+08:00"})
        check("exactly one CC4 dispute effect",
              sum(1 for p in r.json()["pending"]
                  if p["import_id"] == "CC4" and p["reason_code"] == "disputed") == 1)

        # 13d) 解除与出单并发：解除后最终出单能入账（再出单收敛）
        c.post("/play-disputes/DSP-CC/release")
        r = c.post("/statements/issue", json=CC2_PERIOD)
        body = r.json()
        # CC2 解除后按协议 100% => A=100 应出追补单（若并发窗口里已出过则可能无变化）
        check("after release concurrent settled",
              body["kind"] in ("initial", "supplement"))
        pv = c.get("/statements/preview", params=CC2_PERIOD).json()
        check("CC2 finally payable",
              any(x["holder_code"] == "A" and x["amount"] >= 100 for x in pv["payable"])
              and not any(p["import_id"] == "CC2" for p in pv["pending"]),
              str(pv))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL DISPUTE INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
