"""编排拓扑描述：全平台唯一的拓扑解析口径。

历史上编排图节点、依赖边、起始节点在依赖解析、运行实例等入口各解析了一遍，
节点别名与正式节点名冲突时，不同入口套用不同版本的别名映射，同一份拓扑结构
被理解成不同的作业集合。重构后解析只保留这一份：

- 拓扑描述统一为一份 JSON：节点、依赖边、起始节点、别名映射、映射版本；
- 节点别名与正式节点名冲突时正式节点名优先，冲突别名在快照里留痕；
- 每个编排版本规范化时钉住自己导入时的映射版本，较晚导入的映射不会回写历史版本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class TopologyError(ValueError):
    """拓扑描述无法解析或未通过校验，消息面向业务可读。"""


@dataclass(frozen=True)
class TopologyDoc:
    """一份拓扑描述的原始结构：已按 JSON 形状读出，尚未解析别名。"""

    version: str
    mapping_version: str
    nodes: list[str]
    edges: list[tuple[str, str]]
    start_nodes: list[str] | None
    aliases: dict[str, str]


@dataclass(frozen=True)
class NormalizedTopology:
    """规范化结果：别名已解析、依赖边已去重、起始节点已推导并校验无环。"""

    version: str
    mapping_version: str
    nodes: list[str]
    edges: list[tuple[str, str]]
    start_nodes: list[str]
    node_mapping: dict[str, str]
    shadowed_aliases: list[str]


def parse_topology(raw: Any) -> TopologyDoc:
    """把 JSON 字符串或字典读成一份拓扑描述；形状不合法时抛出 TopologyError。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TopologyError(f"拓扑结构不是合法的 JSON 拓扑描述：{exc.msg}") from exc
    if not isinstance(raw, dict):
        raise TopologyError("拓扑结构必须是一份 JSON 对象，包含节点、依赖边、别名映射等字段")

    version = str(raw.get("编排版本") or "1").strip() or "1"
    mapping_version = str(raw.get("映射版本") or version).strip() or version

    nodes_raw = raw.get("节点")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise TopologyError("拓扑描述缺少节点列表，或节点列表为空")
    nodes: list[str] = []
    for item in nodes_raw:
        if not isinstance(item, str) or not item.strip():
            raise TopologyError("节点列表里存在空节点名或非文本节点名")
        name = item.strip()
        if name not in nodes:
            nodes.append(name)

    edges_raw = raw.get("依赖边") or []
    if not isinstance(edges_raw, list):
        raise TopologyError("依赖边必须是列表")
    edges = [_parse_edge(item) for item in edges_raw]

    starts_raw = raw.get("起始节点")
    start_nodes: list[str] | None = None
    if starts_raw is not None:
        if not isinstance(starts_raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in starts_raw
        ):
            raise TopologyError("起始节点必须是节点名列表")
        start_nodes = [item.strip() for item in starts_raw]

    aliases_raw = raw.get("别名映射") or {}
    if not isinstance(aliases_raw, dict):
        raise TopologyError("别名映射必须是「别名: 正式节点名」的对象")
    aliases: dict[str, str] = {}
    for alias, target in aliases_raw.items():
        if not isinstance(alias, str) or not alias.strip():
            raise TopologyError("别名映射里存在空的别名")
        if not isinstance(target, str) or not target.strip():
            raise TopologyError(f"别名「{alias}」指向的正式节点名为空")
        aliases[alias.strip()] = target.strip()

    return TopologyDoc(
        version=version,
        mapping_version=mapping_version,
        nodes=nodes,
        edges=edges,
        start_nodes=start_nodes,
        aliases=aliases,
    )


def normalize_topology(doc: TopologyDoc) -> NormalizedTopology:
    """把一份拓扑描述规范化：解析别名、依赖边去重、推导起始节点并校验无环。"""
    formal = set(doc.nodes)
    for alias, target in doc.aliases.items():
        if target not in formal:
            raise TopologyError(f"别名「{alias}」指向的正式节点「{target}」不在节点列表里")
    shadowed = [alias for alias in doc.aliases if alias in formal]

    def resolve(name: str) -> str:
        # 正式节点名优先于别名：别名与正式节点名冲突时不改写既有指向
        if name in formal:
            return name
        if name in doc.aliases:
            return doc.aliases[name]
        raise TopologyError(f"节点「{name}」既不是正式节点，也没有对应的别名映射")

    resolved: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for upstream, downstream in doc.edges:
        edge = (resolve(upstream), resolve(downstream))
        if edge[0] == edge[1]:
            raise TopologyError(f"依赖边「{upstream} -> {downstream}」解析后指向同一节点，形成自环")
        if edge not in seen:
            seen.add(edge)
            resolved.append(edge)
    resolved.sort()

    start_nodes = _derived_start_nodes(doc.nodes, resolved)

    if doc.start_nodes is not None:
        declared = sorted({resolve(name) for name in doc.start_nodes})
        if declared != sorted(start_nodes):
            raise TopologyError(
                "起始节点与拓扑推导不一致："
                f"声明为 {'、'.join(declared) or '空'}，按依赖边推导为 {'、'.join(start_nodes) or '空'}"
            )

    return NormalizedTopology(
        version=doc.version,
        mapping_version=doc.mapping_version,
        nodes=list(doc.nodes),
        edges=resolved,
        start_nodes=start_nodes,
        node_mapping=dict(doc.aliases),
        shadowed_aliases=shadowed,
    )


def normalize(raw: Any) -> NormalizedTopology:
    """解析加规范化一步完成，供血缘快照统一调用。"""
    return normalize_topology(parse_topology(raw))


def _parse_edge(item: Any) -> tuple[str, str]:
    if isinstance(item, dict):
        upstream = item.get("上游", item.get("上游节点"))
        downstream = item.get("下游", item.get("下游节点"))
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        upstream, downstream = item
    elif isinstance(item, str):
        parts = item.replace("→", "->").split("->")
        if len(parts) != 2:
            raise TopologyError(f"依赖边「{item}」不是合法的「上游->下游」写法")
        upstream, downstream = parts[0], parts[1]
    else:
        raise TopologyError(
            f"依赖边「{item}」无法识别，支持「上游->下游」、[上游, 下游] 或 {{上游, 下游}} 三种写法"
        )
    if not isinstance(upstream, str) or not upstream.strip():
        raise TopologyError(f"依赖边「{item}」的上游节点名为空")
    if not isinstance(downstream, str) or not downstream.strip():
        raise TopologyError(f"依赖边「{item}」的下游节点名为空")
    return upstream.strip(), downstream.strip()


def _derived_start_nodes(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """按依赖边推导起始节点（入度为零），顺带做拓扑排序校验无环。"""
    indegree = {name: 0 for name in nodes}
    downstreams: dict[str, list[str]] = {name: [] for name in nodes}
    for upstream, downstream in edges:
        downstreams[upstream].append(downstream)
        indegree[downstream] += 1
    queue = [name for name in nodes if indegree[name] == 0]
    start_nodes = list(queue)
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in downstreams[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(nodes):
        loop_nodes = [name for name in nodes if indegree[name] > 0]
        raise TopologyError(f"依赖边存在环路，涉及节点：{'、'.join(loop_nodes)}")
    return start_nodes
