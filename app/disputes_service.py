"""播放更正链计费争议（财务暂缓/解除整条更正链的计费依据）。

财务发现某条播放更正链的计费依据存疑时，可凭"链内任一播放编号
anchor_import_id、调用方唯一争议号 dispute_ref 与原因 reason"登记暂缓；
暂缓以更正链原始编号锚定，作用于整条链——无论当前链尾还是暂缓后新增的
更正，暂缓期间该链一律列为争议待处理（disputed），不计入应付；缺协议
与争议并存时以争议为先。凭争议号解除后，出单/试算按最新链尾与适用协议
恢复；解除不改动任何已出具单据与归因（保持冻结），再次出单时按现有
追补规则撤销该链暂缓期间已计金额、恢复后差额无变化则不产生新单。

- 登记暂缓：
  - 同号同内容重试幂等返回原争议（status=duplicate，HTTP 200），不改变状态；
  - 同号异内容（解析出的链或原因不同）409 dispute_ref_conflict；
  - 播放编号未知 404 play_not_found；
  - 同一链已有未解除争议时再次开启 409 dispute_already_open，拒绝且状态不变
    （即使使用新争议号）；
  - 解除后可用新争议号重新开启。
- 解除：只凭争议号；
  - open -> released，落解除操作记录，返回原争议；
  - 重复解除幂等（status=duplicate，HTTP 200）；
  - 未知争议号 404 play_dispute_not_found。
- 查询：返回原始编号、登记用编号、当前链尾、状态、原因与开启/解除操作记录
  （操作记录按发生次序；幂等重试与被拒绝请求不留痕）。

并发协议（与出单互斥，保证基于一致的已提交状态，不重复出单也不漏算）：
- 争议操作：争议号事务咨询锁（同号并发请求串行，幂等/冲突判定确定）
  → 链根事务咨询锁（与出单对链状态的读取互斥）。
- 出单（statements_service）：期间事务咨询锁 → 期间内全部相关更正链的
  链根咨询锁（按原始编号升序，无死锁）→ 重算。
  因此"暂缓/解除"与"出单"在同一链上完全串行：先提交者建立的状态对后到者
  整体可见（READ COMMITTED 语句快照），不会出现"争议已开但该单仍计入
  应付"或"争议已解除但该单仍挂争议待处理"。
- 试算（preview）只读不加锁，与既有约定一致：展示调用瞬间的已提交状态。
"""
from .errors import APIError
from .payments_service import take_ref_advisory_lock

# 争议号事务咨询锁命名空间
_DISPUTE_NS = 0x44535031  # "DSP1"
# 更正链（按原始编号）事务咨询锁命名空间：争议操作与出单在此锁上串行
_CHAIN_NS = 0x43484E31  # "CHN1"

# 递归安全上限（plays.corrects_id 唯一且无环，正常链远短于此）
_CHAIN_DEPTH_LIMIT = 10000

_DISPUTE_COLS = (
    "id, dispute_ref, root_import_id, anchor_import_id, reason, status, "
    "opened_at, created_at, released_at"
)


def _chain_lock(conn, root_import_id: str) -> None:
    """取更正链（原始编号）事务咨询锁。"""
    take_ref_advisory_lock(conn, _CHAIN_NS, root_import_id)


def lock_chain_roots(conn, root_import_ids) -> None:
    """出单侧调用：按原始编号升序逐个锁定期间内相关更正链，再重算/出单。

    与争议操作的链根锁互斥；升序加锁保证多链事务间加锁次序全局一致，无死锁。
    """
    for root in sorted({r for r in root_import_ids if r}):
        _chain_lock(conn, root)


def resolve_chain(conn, import_id: str) -> dict | None:
    """把"链内任一播放编号"解析为更正链描述。

    返回 {"root_id", "tail_id"}；编号未知返回 None。
    播放只追加、从不更新，corrects_id 唯一使链必然线性无分叉，
    因此解析过程不受并发更正影响（见 plays._import_one）。
    """
    row = conn.execute(
        "SELECT import_id, corrects_id FROM plays WHERE import_id = %s",
        (import_id,),
    ).fetchone()
    if row is None:
        return None

    # 沿 corrects_id 回溯到原始记录
    root_id = import_id
    depth = 0
    while row["corrects_id"] is not None:
        root_id = row["corrects_id"]
        row = conn.execute(
            "SELECT import_id, corrects_id FROM plays WHERE import_id = %s",
            (root_id,),
        ).fetchone()
        depth += 1
        if depth > _CHAIN_DEPTH_LIMIT or row is None:
            raise APIError(500, "chain_too_long", "correction chain too long")

    # 从原始记录向后走到当前链尾（无后继者即链尾）
    tail_id = root_id
    depth = 0
    while True:
        nxt = conn.execute(
            "SELECT import_id FROM plays WHERE corrects_id = %s", (tail_id,)
        ).fetchone()
        if nxt is None:
            break
        tail_id = nxt["import_id"]
        depth += 1
        if depth > _CHAIN_DEPTH_LIMIT:
            raise APIError(500, "chain_too_long", "correction chain too long")

    return {"root_id": root_id, "tail_id": tail_id}


def _load_events(conn, dispute_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_type, play_import_id, reason, created_at
        FROM play_dispute_events
        WHERE dispute_id = %s
        ORDER BY id
        """,
        (dispute_id,),
    ).fetchall()
    return [
        {
            "operation": r["event_type"],
            "play_import_id": r["play_import_id"],
            "reason": r["reason"],
            "at": r["created_at"],
        }
        for r in rows
    ]


def _payload(conn, row, *, request_status: str, chain: dict | None = None) -> dict:
    """组装争议响应。查询/重试时链尾按当前数据解析（链尾随后续更正前移）。"""
    if chain is None:
        chain = resolve_chain(conn, row["root_import_id"]) or {
            "root_id": row["root_import_id"], "tail_id": row["root_import_id"],
        }
    return {
        "dispute_ref": row["dispute_ref"],
        "root_import_id": row["root_import_id"],
        "anchor_import_id": row["anchor_import_id"],
        "current_tail_import_id": chain["tail_id"],
        # 请求级状态：created=本次开启；released=本次解除；
        # duplicate=同号重试开启或重复解除；查询时与争议自身状态一致
        "status": request_status,
        "dispute_status": row["status"],
        "reason": row["reason"],
        "opened_at": row["opened_at"],
        "released_at": row["released_at"],
        "operations": _load_events(conn, row["id"]),
    }


def open_dispute(conn, play_import_id: str, dispute_ref: str, reason: str) -> dict:
    """对播放所在更正链登记计费争议暂缓。

    返回 (payload, http_status)：新开启 201，同号同内容重试 200。
    """
    with conn.transaction():
        # 1) 争议号咨询锁：同号并发请求串行化。
        take_ref_advisory_lock(conn, _DISPUTE_NS, dispute_ref)

        # 2) 解析更正链（未知播放 404；整次拒绝，不留争议）。
        #    先于同号判定解析，使"链内任一编号"都能命中原争议（内容按链归一）。
        chain = resolve_chain(conn, play_import_id)
        if chain is None:
            raise _play_not_found(play_import_id)

        existing = conn.execute(
            f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE dispute_ref = %s",
            (dispute_ref,),
        ).fetchone()
        if existing is not None:
            # 内容按"解析后的更正链 + 原因"判定：用链内原始编号或链尾编号重试
            # 都视为同内容；异链或异原因才是异内容复用。
            same_content = (
                existing["root_import_id"] == chain["root_id"]
                and existing["reason"] == reason
            )
            if not same_content:
                raise _ref_conflict(dispute_ref, existing)
            # 同号同内容重试：无论 open/released 都幂等返回原结果，不改变状态。
            return _payload(conn, existing, request_status="duplicate")

        # 3) 链根锁：与同链的出单、其他争议开启/解除串行。
        _chain_lock(conn, chain["root_id"])

        open_row = conn.execute(
            """
            SELECT id, dispute_ref FROM play_disputes
            WHERE root_import_id = %s AND status = 'open'
            """,
            (chain["root_id"],),
        ).fetchone()
        if open_row is not None:
            raise _already_open(chain["root_id"], open_row)

        row = conn.execute(
            """
            INSERT INTO play_disputes
                (dispute_ref, root_import_id, anchor_import_id, reason)
            VALUES (%s, %s, %s, %s)
            RETURNING id, opened_at, created_at
            """,
            (dispute_ref, chain["root_id"], play_import_id, reason),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO play_dispute_events
                (dispute_id, event_type, play_import_id, reason)
            VALUES (%s, 'opened', %s, %s)
            """,
            (row["id"], play_import_id, reason),
        )

        created = conn.execute(
            f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, created, request_status="created", chain=chain)


def release_dispute(conn, dispute_ref: str) -> dict:
    """凭争议号解除暂缓（幂等：重复解除返回原争议，状态不变）。"""
    with conn.transaction():
        # 1) 争议号咨询锁：同号并发解除串行。
        take_ref_advisory_lock(conn, _DISPUTE_NS, dispute_ref)

        # 2) 锁争议行：与一切状态判定互斥。
        row = conn.execute(
            f"SELECT {_DISPUTE_COLS} FROM play_disputes "
            "WHERE dispute_ref = %s FOR UPDATE",
            (dispute_ref,),
        ).fetchone()
        if row is None:
            raise _dispute_not_found(dispute_ref)

        if row["status"] == "released":
            return _payload(conn, row, request_status="duplicate")

        # 3) 链根锁：与出单重算串行——解除提交前出单看到 open（继续挂争议），
        #    解除提交后出单看到 released（按最新链尾恢复），不混合前后状态。
        _chain_lock(conn, row["root_import_id"])

        updated = conn.execute(
            """
            UPDATE play_disputes
            SET status = 'released', released_at = now()
            WHERE id = %s AND status = 'open'
            RETURNING id
            """,
            (row["id"],),
        ).fetchone()
        if updated is None:  # 行锁下理论不可达，防御性兜底
            refreshed = conn.execute(
                f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE id = %s",
                (row["id"],),
            ).fetchone()
            if refreshed is not None and refreshed["status"] == "released":
                return _payload(conn, refreshed, request_status="duplicate")
            raise APIError(
                409, "dispute_not_open",
                f"play dispute {dispute_ref} is not open; release rejected",
            )
        conn.execute(
            """
            INSERT INTO play_dispute_events
                (dispute_id, event_type, play_import_id, reason)
            VALUES (%s, 'released', %s, %s)
            """,
            (row["id"], row["anchor_import_id"], row["reason"]),
        )

        released = conn.execute(
            f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _payload(conn, released, request_status="released")


def get_dispute(conn, dispute_ref: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_DISPUTE_COLS} FROM play_disputes WHERE dispute_ref = %s",
        (dispute_ref,),
    ).fetchone()
    if row is None:
        return None
    # 查询为只读：当前链尾随后续更正实时解析。
    return _payload(conn, row, request_status=row["status"])


def _play_not_found(play_import_id: str):
    return APIError(
        404,
        "play_not_found",
        f"play {play_import_id} not found; cannot open a chain dispute",
        detail={"play_import_id": play_import_id},
    )


def _dispute_not_found(dispute_ref: str):
    return APIError(
        404,
        "play_dispute_not_found",
        f"play dispute {dispute_ref} not found",
        detail={"dispute_ref": dispute_ref},
    )


def _ref_conflict(dispute_ref: str, existing):
    return APIError(
        409,
        "dispute_ref_conflict",
        f"dispute ref {dispute_ref} already registered with different content",
        detail={
            "dispute_ref": dispute_ref,
            "existing_play_import_id": existing["anchor_import_id"],
            "existing_reason": existing["reason"],
            "existing_status": existing["status"],
        },
    )


def _already_open(root_import_id: str, existing):
    return APIError(
        409,
        "dispute_already_open",
        f"correction chain rooted at {root_import_id} already has open dispute "
        f"{existing['dispute_ref']}; duplicate open rejected and state unchanged",
        detail={
            "root_import_id": root_import_id,
            "existing_dispute_ref": existing["dispute_ref"],
        },
    )
