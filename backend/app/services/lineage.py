"""数据表依赖血缘快照：把历史编排图拓扑记录规范化成统一快照。

写入纪律：
- 节点映射与依赖边去重在内存里一次算好，整份快照一次提交；解析或校验
  任何一步失败都不写库，上一份完整血缘快照原样保留（原子写入）；
- 同一（图编号, 编排版本）只生成一份快照，重复规范化幂等返回既有记录；
- 提交前在锁内复查唯一键，并发导入同一编排版本只有一份快照生效。

读取纪律：依赖解析与运行实例都从这里取执行集合，保证两处看到同一组
节点与上下游边。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.services.topology import NormalizedTopology, TopologyError, normalize
from app.store import store

MODULE = "lineage"


class LineageService:
    def __init__(self) -> None:
        self._commit_lock = threading.Lock()

    def list_snapshots(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("图编号", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, snapshot_id)

    def normalize_dag(
        self,
        *,
        graph_code: Any,
        topology: Any = None,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        """把一份编排图拓扑规范化为血缘快照；返回 (快照, 是否新建, 说明)。"""
        graph_code = str(graph_code or "").strip()
        if not graph_code:
            return None, False, "缺少必填字段：图编号"
        if topology is None:
            dag = self._find_dag(graph_code)
            if dag is None:
                return None, False, f"编排图 {graph_code} 不存在或已归档，且未提供拓扑结构"
            topology = dag.get("拓扑结构")
        try:
            normalized = normalize(topology)
        except TopologyError as exc:
            # 解析或校验任何一步失败都不写库，上一份完整快照原样保留
            return None, False, f"拓扑规范化失败，已保留上一份完整血缘快照：{exc}"
        with self._commit_lock:
            existing = self._find_locked(graph_code, normalized.version)
            if existing is not None:
                return existing, False, "同一编排版本已生成过血缘快照，返回既有记录"
            rows = store.rows(MODULE)
            snapshot = self._build_snapshot(graph_code, normalized)
            snapshot["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            snapshot["快照编号"] = f"LINE-{snapshot['id']:04d}"
            rows.append(snapshot)
            return snapshot, True, "血缘快照已生成"

    def execution_set(
        self,
        *,
        graph_code: str,
        version: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """统一读取执行集合：依赖解析与运行实例共用这一份快照数据。"""
        graph_code = str(graph_code or "").strip()
        rows = [row for row in store.rows(MODULE) if row.get("图编号") == graph_code]
        if version is not None:
            rows = [row for row in rows if str(row.get("编排版本")) == str(version)]
        if not rows:
            return None, f"编排图 {graph_code} 还没有生效的血缘快照，请先规范化"
        snapshot = max(rows, key=lambda row: int(row.get("id", 0)))
        return {
            "快照编号": snapshot["快照编号"],
            "图编号": snapshot["图编号"],
            "编排版本": snapshot["编排版本"],
            "节点列表": list(snapshot["节点列表"]),
            "依赖边": [dict(edge) for edge in snapshot["依赖边"]],
            "起始节点": list(snapshot["起始节点"]),
        }, "ok"

    def _find_dag(self, graph_code: str) -> dict[str, Any] | None:
        for row in store.rows("dag"):
            if row.get("图编号") == graph_code:
                return row
        return None

    def _find_locked(self, graph_code: str, version: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("图编号") == graph_code and str(row.get("编排版本")) == str(version):
                return row
        return None

    @staticmethod
    def _build_snapshot(graph_code: str, topo: NormalizedTopology) -> dict[str, Any]:
        return {
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "图编号": graph_code,
            "编排版本": topo.version,
            "映射版本": topo.mapping_version,
            "节点映射": dict(topo.node_mapping),
            "冲突别名": list(topo.shadowed_aliases),
            "节点列表": list(topo.nodes),
            "依赖边": [
                {"上游节点": upstream, "下游节点": downstream}
                for upstream, downstream in topo.edges
            ],
            "起始节点": list(topo.start_nodes),
            "节点数": len(topo.nodes),
            "依赖边数": len(topo.edges),
            "生成时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


lineage_service = LineageService()
