"""播放争议暂缓（财务对更正链计费依据存疑时登记/解除）。

财务凭"链内任一播放编号 + 调用方唯一争议号 dispute_ref + 原因"登记暂缓，
凭争议号解除；查询回显更正链原始编号、当前链尾、状态与操作记录。

语义约定：
- 争议绑定开启时回溯固化的更正链原始编号 root_import_id，作用于整条
  更正链：后续更正只是延长链（链根不变），争议自动继承到新的链尾；
- 未解除期间，试算与出单把当前链尾列为"争议待处理"、不计应付
  （缺协议与争议同时成立时以争议为先，见 statements_service）；
- 同号同内容重试幂等返回原记录（status=duplicate，HTTP 200）；
  同号异内容（凭据编号或原因不同）409 dispute_ref_conflict；
- 未知播放 404 play_not_found；同链已有未解除争议 409 dispute_already_open，
  拒绝均不改变任何状态；
- 解除幂等：重复解除返回原记录（status=duplicate，HTTP 200）；
  解除后可凭新争议号重新开启（旧争议号重试仍只返回原已解除记录）。

并发协议（与出单/其他争议操作一致、无死锁）：
- 开启先取"争议号事务咨询锁"（同号并发串行，恰一笔创建，其余幂等/冲突），
  再取"更正链根事务咨询锁"（同链并发开启串行，"同链重复开启"判定确定）；
- 解除取"争议号事务咨询锁"后锁定争议行（FOR UPDATE）再迁移状态；
- 出单侧在单条 SQL（含未解除争议左连接）的语句快照内同时读取播放与争议
  状态，争议开启/解除与出单并发时各自基于一致的已提交状态，
  不会重复出单或漏算。
"""

from .errors import APIError
from .payments_service import take_ref_advisory_lock

# 咨询锁命名空间（高 32 位）
_DISPUTE_REF_NS = 0x44535031    # "DSP1"：争议号
_DISPUTE_CHAIN_NS = 0x44535032  # "DSP2"：更正链根（同链开启串行化）

_DISPUTE_COLS = (
    "id, dispute_ref, root_import_id, open_import_id, reason,"
    " status, opened_at, released_at"
)

# 从链内任一编号回溯到更正链原始编号（corrects_id 为空的链根）
_ROOT_SQL = """
WITH RECURSIVE walk AS (
    SELECT import_id, corrects_id, 0 AS depth
    FROM plays
    WHERE import_id = %(import_id)s
    UNION ALL
    SELECT p.import_id, p.corrects_id, w.depth + 1
    FROM walk w
    JOIN plays p ON p.import_id = w.corrects_id
    WHERE w.depth < 10000
)
SELECT import_id FROM walk WHERE corrects_id IS NULL
"""

# 从链根顺推到当前链尾（corrects_id 唯一，链必然线性）
_TAIL_SQL = """
WITH RECURSIVE walk AS (
    SELECT import_id, 0 AS depth
    FROM plays
    WHERE import_id = %(root)s
    UNION ALL
    SELECT p.import_id, w.depth + 1
    FROM walk w
    JOIN plays p ON p.corrects_id = w.import_id
    WHERE w.depth < 10000
)
SELECT import_id FROM walk ORDER BY depth DESC LIMIT 1
"""


def _current_tail(conn, root_import_id: str) -> str:
    row = conn.execute(_TAIL_SQL, {"root": root_import_id}).fetchone()
    return row["import_id"]


def _payload(conn, row: dict, *, status: str) -> dict:
    """组装响应：当前链尾实时顺推（后续更正自动继承争议），操作记录按序追加。"""
    events = conn.execute(
        """
        SELECT action, note, created_at
        FROM play_dispute_events
        WHERE dispute_id = %s
        ORDER BY id
        """,
        (row["id"],),
    ).fetchall()
    return {
        "dispute_id": row["id"],
        "dispute_ref": row["dispute_ref"],
        "root_import_id": row["root_import_id"],
        "open_import_id": row["open_import_id"],
        "tail_import_id": _current_tail(conn, row["root_import_id"]),
        "reason": row["reason"],
        "status": status,
        "dispute_status": row["status"],
        "opened_at": row["opened_at"],
        "released_at": row["released_at"],
        "events": [
            {"action": e["action"], "note": e["note"], "created_at": e["created_at"]}
            for e in events
        ],
    }


def open_dispute(conn, import_id: str, dispute_ref: str, reason: str) -> dict:
    """登记争议暂缓。

    - 未知播放 404 play_not_found；同链已有未解除争议 409 dispute_already_open；
    - 同号同内容重试返回原记录（status=duplicate）；同号异内容
      409 dispute_ref_conflict；上述拒绝均不改变任何状态。
    """
    with conn.transaction():
        # 争议号咨询锁：同号并发请求串行化（恰一笔创建，其余幂等/冲突）
        take_ref_advisory_lock(conn, _DISPUTE_REF_NS, dispute_ref)

        existing = conn.execute(
            f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE dispute_ref = %s",
            (dispute_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["open_import_id"] != import_id
                or existing["reason"] != reason
            ):
                raise APIError(
                    409,
                    "dispute_ref_conflict",
                    f"dispute ref {dispute_ref} already registered with "
                    "different content",
                    detail={
                        "dispute_ref": dispute_ref,
                        "existing_open_import_id": existing["open_import_id"],
                        "existing_reason": existing["reason"],
                        "existing_status": existing["status"],
                    },
                )
            # 同号同内容重试：返回原记录（即使已解除也不重开）
            return _payload(conn, existing, status="duplicate")

        play = conn.execute(
            "SELECT import_id FROM plays WHERE import_id = %s",
            (import_id,),
        ).fetchone()
        if play is None:
            raise APIError(
                404, "play_not_found", f"play {import_id} not found"
            )

        root_id = conn.execute(
            _ROOT_SQL, {"import_id": import_id}
        ).fetchone()["import_id"]

        # 链根咨询锁：同链并发开启串行化，"同链重复开启"判定确定
        take_ref_advisory_lock(conn, _DISPUTE_CHAIN_NS, root_id)

        open_row = conn.execute(
            """
            SELECT dispute_ref, open_import_id, opened_at
            FROM play_disputes
            WHERE root_import_id = %s AND status = 'open'
            """,
            (root_id,),
        ).fetchone()
        if open_row is not None:
            raise APIError(
                409,
                "dispute_already_open",
                f"correction chain rooted at {root_id} already has open "
                f"dispute {open_row['dispute_ref']}; request rejected",
                detail={
                    "root_import_id": root_id,
                    "existing_dispute_ref": open_row["dispute_ref"],
                    "existing_open_import_id": open_row["open_import_id"],
                },
            )

        row = conn.execute(
            f"""
            INSERT INTO play_disputes
                (dispute_ref, root_import_id, open_import_id, reason)
            VALUES (%s, %s, %s, %s)
            RETURNING {_DISPUTE_COLS}
            """,
            (dispute_ref, root_id, import_id, reason),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO play_dispute_events (dispute_id, action, note)
            VALUES (%s, 'open', %s)
            """,
            (row["id"], reason),
        )
        return _payload(conn, row, status="created")


def release_dispute(conn, dispute_ref: str) -> dict:
    """凭争议号解除暂缓。

    重复解除幂等（status=duplicate，HTTP 200）；未知争议号
    404 dispute_not_found。解除后该链恢复按最新链尾与适用协议核算，
    且可凭新争议号重新开启。
    """
    with conn.transaction():
        # 争议号咨询锁：同号并发解除/开启串行化
        take_ref_advisory_lock(conn, _DISPUTE_REF_NS, dispute_ref)

        row = conn.execute(
            f"SELECT {_DISPUTE_COLS} FROM play_disputes "
            "WHERE dispute_ref = %s FOR UPDATE",
            (dispute_ref,),
        ).fetchone()
        if row is None:
            raise APIError(
                404,
                "dispute_not_found",
                f"play dispute {dispute_ref} not found",
            )
        if row["status"] == "released":
            # 重复解除幂等：返回原记录，不重复落操作记录
            return _payload(conn, row, status="duplicate")

        row = conn.execute(
            f"""
            UPDATE play_disputes
            SET status = 'released', released_at = now()
            WHERE id = %s
            RETURNING {_DISPUTE_COLS}
            """,
            (row["id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO play_dispute_events (dispute_id, action, note)
            VALUES (%s, 'release', NULL)
            """,
            (row["id"],),
        )
        return _payload(conn, row, status="released")


def get_dispute(conn, dispute_ref: str) -> dict | None:
    """按争议号查询：原始编号、当前链尾、状态与操作记录。

    未知争议号返回 None（路由层 404）。查询不改变任何状态。
    """
    row = conn.execute(
        f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE dispute_ref = %s",
        (dispute_ref,),
    ).fetchone()
    if row is None:
        return None
    # 查询时 status 即争议自身状态（与创建/解除响应同形）
    return _payload(conn, row, status=row["status"])
