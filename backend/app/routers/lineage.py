"""血缘快照接口：历史编排图拓扑规范化与统一血缘快照查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.lineage import lineage_service

router = APIRouter(prefix="/api/lineage", tags=["血缘快照"])

service = lineage_service


@router.get("", response_model=PageResult[dict])
def list_snapshots(
    keyword: str | None = Query(default=None, description="按图编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按图编号过滤血缘快照列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_snapshots(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/normalize", response_model=ActionResult)
def normalize_snapshot(payload: EntryPayload) -> ActionResult:
    """把一份编排图拓扑规范化为血缘快照。

    同一（图编号, 编排版本）重复规范化只保留一份记录；解析或校验失败时
    不写库，上一份完整血缘快照原样保留。
    """
    snapshot, _created, message = service.normalize_dag(
        graph_code=payload.values.get("图编号"),
        topology=payload.values.get("拓扑结构"),
    )
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int) -> dict:
    """读取单条血缘快照明细；不存在时给出可读的错误说明。"""
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"血缘快照 {snapshot_id} 不存在或已归档")
    return snapshot
