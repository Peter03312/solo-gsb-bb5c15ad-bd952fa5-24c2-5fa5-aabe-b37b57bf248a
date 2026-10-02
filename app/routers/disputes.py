from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import disputes_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import DisputeOpenIn, DisputeOut

router = APIRouter(tags=["play-disputes"])


@router.post("/plays/disputes")
def open_dispute(body: DisputeOpenIn, conn=Depends(get_conn)):
    """登记播放争议暂缓（财务对某条更正链的计费依据存疑）。

    - 凭链内任一播放编号 + 唯一争议号 + 原因登记；争议作用于整条更正链，
      后续更正自动继承；未解除期间试算/出单把当前链尾列为争议待处理、
      不计应付（缺协议与争议同时成立时以争议为先）；
    - 同号同内容重试幂等返回原记录（status=duplicate，HTTP 200）；
      同号异内容（凭据编号或原因不同）409 dispute_ref_conflict；
    - 未知播放 404 play_not_found；同链已有未解除争议
      409 dispute_already_open；拒绝均不改变任何状态。
    """
    result = svc.open_dispute(conn, body.import_id, body.dispute_ref, body.reason)
    # 新开启 201；同号同内容重试 200 且返回原记录
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=DisputeOut(**result).model_dump(mode="json"),
    )


@router.post("/plays/disputes/{dispute_ref}/release")
def release_dispute(dispute_ref: str, conn=Depends(get_conn)):
    """凭争议号解除暂缓。

    - 解除后该链恢复按最新链尾与适用协议核算（缺协议仍列待处理），
      可凭新争议号重新开启；
    - 重复解除幂等返回原记录（status=duplicate，HTTP 200）；
      未知争议号 404 dispute_not_found。
    """
    result = svc.release_dispute(conn, dispute_ref)
    # 本次解除 200（无新建资源）；重复解除幂等也 200
    return JSONResponse(
        status_code=200,
        content=DisputeOut(**result).model_dump(mode="json"),
    )


@router.get("/plays/disputes/{dispute_ref}", response_model=DisputeOut)
def get_dispute(dispute_ref: str, conn=Depends(get_conn)):
    """按争议号查询：更正链原始编号、当前链尾、状态与操作记录。

    当前链尾实时顺推（后续更正自动继承争议）；查询不改变任何状态。
    未知争议号返回 404 dispute_not_found。
    """
    result = svc.get_dispute(conn, dispute_ref)
    if result is None:
        raise APIError(
            404,
            "dispute_not_found",
            f"play dispute {dispute_ref} not found",
        )
    return result
