from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import disputes_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import PlayDisputeIn, PlayDisputeOut

router = APIRouter(tags=["play-disputes"])


@router.post("/play-disputes")
def open_dispute(body: PlayDisputeIn, conn=Depends(get_conn)):
    """对某条播放更正链登记计费争议暂缓（暂缓作用于整条链）。

    - 凭链内任一播放编号 play_import_id、唯一争议号 dispute_ref 与原因
      reason 登记；暂缓以更正链原始编号锚定，链尾参与试算/出单时列为
      "争议待处理"(reason_code=disputed)，整链不计应付；暂缓后新增的
      更正自动继承暂缓；缺协议与争议并存时以争议为先；
    - 同号同内容重试幂等返回原争议（status=duplicate，HTTP 200，不改变状态）；
      同号异内容（链内编号或原因不同）409 dispute_ref_conflict；
    - 播放编号未知 404 play_not_found；同一链已有未解除争议（含使用新争议号）
      409 dispute_already_open，拒绝且状态不变；解除后可用新争议号重开。
    """
    result = svc.open_dispute(conn, body.play_import_id, body.dispute_ref, body.reason)
    # 新开启 201；同号同内容重试 200 且返回原争议
    status_code = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=PlayDisputeOut(**result).model_dump(mode="json"),
    )


@router.post("/play-disputes/{dispute_ref}/release", response_model=PlayDisputeOut)
def release_dispute(dispute_ref: str, conn=Depends(get_conn)):
    """凭争议号解除暂缓。

    - open -> released：解除后试算/出单按最新链尾与适用协议恢复，缺协议仍
      待处理；已出具单据与归因保持冻结，再次出单按现有追补规则撤销该链已计
      金额，应付无变化不产生新单；
    - 重复解除幂等（status=duplicate，HTTP 200，原争议状态不变）；
    - 未知争议号 404 play_dispute_not_found。
    """
    result = svc.release_dispute(conn, dispute_ref)
    return PlayDisputeOut(**result).model_dump(mode="json")


@router.get("/play-disputes/{dispute_ref}", response_model=PlayDisputeOut)
def get_dispute(dispute_ref: str, conn=Depends(get_conn)):
    """查询争议：原始编号、当前链尾、状态、原因与开启/解除操作记录。

    未知争议号返回 404 play_dispute_not_found。
    """
    result = svc.get_dispute(conn, dispute_ref)
    if result is None:
        raise APIError(
            404,
            "play_dispute_not_found",
            f"play dispute {dispute_ref} not found",
        )
    return result
