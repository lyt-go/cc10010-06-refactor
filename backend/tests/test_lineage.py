"""血缘快照重构的核对测试。

覆盖交付的可核对结果：血缘快照数据中，依赖解析和运行实例看到相同的
节点与上下游边；以及支撑它的写入纪律——映射版本钉住、原子写入、幂等、
并发唯一，和既有模块行为不变。
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.lineage import lineage_service
from app.store import store

client = TestClient(app)


def make_topology(version, nodes, edges, aliases=None, starts=None, mapping_version=None):
    doc = {"编排版本": version, "节点": nodes, "依赖边": edges}
    if aliases is not None:
        doc["别名映射"] = aliases
    if starts is not None:
        doc["起始节点"] = starts
    if mapping_version is not None:
        doc["映射版本"] = mapping_version
    return doc


@pytest.fixture(autouse=True)
def clean_lineage_table():
    store.rows("lineage").clear()
    yield
    store.rows("lineage").clear()


def normalize(graph_code, topology=None):
    values = {"图编号": graph_code}
    if topology is not None:
        values["拓扑结构"] = topology
    return client.post("/api/lineage/normalize", json={"values": values})


# ---------- 统一解析与快照内容 ----------

def test_normalize_builds_snapshot_with_canonical_topology():
    resp = normalize("DAG-T-1001", make_topology(
        version="1",
        nodes=["JOB_A", "JOB_B", "JOB_C"],
        edges=[
            {"上游": "JOB_A", "下游": "JOB_B"},
            ["JOB_B", "JOB_C"],
            "JOB_A->JOB_C",
            "JOB_A→JOB_C",  # 另一种写法的重复边，去重后只留一条
        ],
    ))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    snap = body["entry"]
    assert snap["图编号"] == "DAG-T-1001"
    assert snap["编排版本"] == "1"
    assert snap["节点列表"] == ["JOB_A", "JOB_B", "JOB_C"]
    assert snap["依赖边"] == [
        {"上游节点": "JOB_A", "下游节点": "JOB_B"},
        {"上游节点": "JOB_A", "下游节点": "JOB_C"},
        {"上游节点": "JOB_B", "下游节点": "JOB_C"},
    ]
    assert snap["起始节点"] == ["JOB_A"]
    assert snap["快照编号"] == "LINE-0001"


def test_alias_mapping_applies_to_edges_and_starts():
    resp = normalize("DAG-T-1002", make_topology(
        version="1",
        nodes=["JOB_A", "JOB_B"],
        edges=["alias_a->JOB_B"],
        aliases={"alias_a": "JOB_A"},
        starts=["alias_a"],
    ))
    snap = resp.json()["entry"]
    assert snap["依赖边"] == [{"上游节点": "JOB_A", "下游节点": "JOB_B"}]
    assert snap["起始节点"] == ["JOB_A"]
    assert snap["节点映射"] == {"alias_a": "JOB_A"}


def test_alias_conflict_with_formal_name_prefers_formal_node():
    resp = normalize("DAG-T-1003", make_topology(
        version="1",
        nodes=["JOB_A", "JOB_B"],
        edges=["JOB_A->JOB_B"],
        aliases={"JOB_A": "JOB_B", "alias_a": "JOB_A"},
        mapping_version="7",
    ))
    snap = resp.json()["entry"]
    # 别名 JOB_A 与正式节点名冲突，正式节点名优先；若按别名改写会成 JOB_B->JOB_B 自环
    assert snap["依赖边"] == [{"上游节点": "JOB_A", "下游节点": "JOB_B"}]
    assert snap["冲突别名"] == ["JOB_A"]
    assert snap["节点映射"] == {"JOB_A": "JOB_B", "alias_a": "JOB_A"}
    assert snap["映射版本"] == "7"


# ---------- 映射版本：不能只按最后一次导入覆盖，旧映射承接历史编排图 ----------

def test_mapping_version_pinned_per_dag_version_and_history_kept():
    v1 = make_topology(
        version="1", nodes=["JOB_A", "JOB_B"], edges=["UP->JOB_B"],
        aliases={"UP": "JOB_A"}, mapping_version="1",
    )
    v2 = make_topology(
        version="2", nodes=["JOB_A", "JOB_B", "JOB_C"], edges=["UP->JOB_B"],
        aliases={"UP": "JOB_C"}, mapping_version="2",
    )
    snap_v2 = normalize("DAG-T-1004", v2).json()["entry"]  # 较晚导入的映射先进入
    snap_v1 = normalize("DAG-T-1004", v1).json()["entry"]  # 历史版本后规范化，仍用自己的旧映射
    assert snap_v1["依赖边"] == [{"上游节点": "JOB_A", "下游节点": "JOB_B"}]
    assert snap_v2["依赖边"] == [{"上游节点": "JOB_C", "下游节点": "JOB_B"}]
    # 旧映射继续承接历史编排图：重复规范化 v1 不会被新映射覆盖
    again = normalize("DAG-T-1004", v1).json()["entry"]
    assert again["id"] == snap_v1["id"]
    assert again["节点映射"] == {"UP": "JOB_A"}
    assert again["依赖边"] == [{"上游节点": "JOB_A", "下游节点": "JOB_B"}]
    assert len(store.rows("lineage")) == 2


# ---------- 幂等：同一编排版本重复规范化只生成一份记录 ----------

def test_same_version_renormalized_yields_single_record():
    topo = make_topology(version="1", nodes=["JOB_A"], edges=[])
    first = normalize("DAG-T-1005", topo).json()
    second = normalize("DAG-T-1005", topo).json()
    assert first["ok"] and second["ok"]
    assert first["entry"]["id"] == second["entry"]["id"]
    assert "已生成过" in second["message"]
    assert len(store.rows("lineage")) == 1


# ---------- 原子写入：任何一步失败都保留上一份完整血缘快照 ----------

@pytest.mark.parametrize("bad_topology", [
    pytest.param(
        make_topology(version="2", nodes=["JOB_A"], edges=["JOB_A->GHOST"]),
        id="未知节点",
    ),
    pytest.param(
        make_topology(version="2", nodes=["JOB_A", "JOB_B"], edges=["JOB_A->JOB_B", "JOB_B->JOB_A"]),
        id="依赖环路",
    ),
    pytest.param(
        make_topology(version="2", nodes=["JOB_A", "JOB_B"], edges=["alias_a->JOB_B"], aliases={"alias_a": "JOB_B"}),
        id="别名解析成自环",
    ),
    pytest.param(
        make_topology(version="2", nodes=["JOB_A", "JOB_B"], edges=["JOB_A->JOB_B"], starts=["JOB_B"]),
        id="起始节点与推导不一致",
    ),
    pytest.param(
        make_topology(version="2", nodes=["JOB_A"], edges=[], aliases={"x": "GHOST"}),
        id="别名指向未知正式节点",
    ),
    pytest.param("not-a-json-topology", id="非法JSON拓扑"),
])
def test_failed_normalize_keeps_previous_snapshot_intact(bad_topology):
    good = make_topology(version="1", nodes=["JOB_A", "JOB_B"], edges=["JOB_A->JOB_B"])
    snap = normalize("DAG-T-1006", good).json()["entry"]
    before = [dict(row) for row in store.rows("lineage")]

    resp = normalize("DAG-T-1006", bad_topology)
    assert resp.json()["ok"] is False
    assert "已保留上一份完整血缘快照" in resp.json()["message"]

    after = store.rows("lineage")
    assert len(after) == len(before) == 1
    assert after[0] == before[0] == snap


# ---------- 并发：同一编排版本只允许一份快照生效 ----------

def test_concurrent_import_of_same_version_yields_single_snapshot():
    topo = make_topology(version="1", nodes=["JOB_A", "JOB_B"], edges=["JOB_A->JOB_B"])
    results = []
    errors = []

    def worker():
        try:
            results.append(lineage_service.normalize_dag(graph_code="DAG-T-1007", topology=topo))
        except Exception as exc:  # noqa: BLE001 - 测试要收集一切异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 16
    assert sum(1 for _snap, created, _msg in results if created) == 1
    assert len({snap["id"] for snap, _created, _msg in results}) == 1
    assert len(store.rows("lineage")) == 1


# ---------- 可核对结果：两个入口看到相同的节点与上下游边 ----------

def test_dependency_and_runtime_read_same_execution_set():
    normalize("DAG-T-1008", make_topology(
        version="1",
        nodes=["JOB_A", "JOB_B", "JOB_C"],
        edges=["alias_a->JOB_B", "JOB_B->JOB_C"],
        aliases={"alias_a": "JOB_A"},
    ))
    dep = client.get("/api/dependency/execution-set", params={"graph_code": "DAG-T-1008"})
    run = client.get("/api/runtime/execution-set", params={"graph_code": "DAG-T-1008"})
    assert dep.status_code == run.status_code == 200
    assert dep.json() == run.json()
    assert dep.json()["节点列表"] == ["JOB_A", "JOB_B", "JOB_C"]
    assert dep.json()["依赖边"] == [
        {"上游节点": "JOB_A", "下游节点": "JOB_B"},
        {"上游节点": "JOB_B", "下游节点": "JOB_C"},
    ]
    assert dep.json()["起始节点"] == ["JOB_A"]


def test_execution_set_defaults_to_latest_and_honors_version():
    normalize("DAG-T-1009", make_topology(version="1", nodes=["JOB_A"], edges=[]))
    normalize("DAG-T-1009", make_topology(version="2", nodes=["JOB_A", "JOB_B"], edges=["JOB_A->JOB_B"]))
    latest = client.get("/api/dependency/execution-set", params={"graph_code": "DAG-T-1009"}).json()
    old = client.get("/api/runtime/execution-set", params={"graph_code": "DAG-T-1009", "version": "1"}).json()
    assert latest["编排版本"] == "2"
    assert latest["节点列表"] == ["JOB_A", "JOB_B"]
    assert old["编排版本"] == "1"
    assert old["节点列表"] == ["JOB_A"]


def test_execution_set_404_without_snapshot():
    resp = client.get("/api/dependency/execution-set", params={"graph_code": "DAG-NONE"})
    assert resp.status_code == 404
    resp = client.get("/api/runtime/execution-set", params={"graph_code": "DAG-NONE"})
    assert resp.status_code == 404


# ---------- 历史编排图记录规范化 ----------

def test_normalize_reads_topology_from_dag_record_when_not_provided():
    topo = make_topology(version="3", nodes=["JOB_A", "JOB_B"], edges=["JOB_A->JOB_B"])
    create = client.post("/api/dag", json={"values": {
        "图编号": "DAG-T-1010",
        "图名称": "测试编排",
        "拓扑结构": json.dumps(topo, ensure_ascii=False),
    }})
    assert create.json()["ok"] is True
    resp = normalize("DAG-T-1010")
    assert resp.json()["ok"] is True
    assert resp.json()["entry"]["编排版本"] == "3"
    assert resp.json()["entry"]["依赖边"] == [{"上游节点": "JOB_A", "下游节点": "JOB_B"}]


def test_normalize_legacy_placeholder_topology_fails_without_touching_store():
    resp = normalize("DAG-0001")  # 种子数据里的历史占位拓扑，不是合法拓扑描述
    assert resp.json()["ok"] is False
    assert store.rows("lineage") == []


def test_normalize_missing_graph_code_rejected():
    resp = client.post("/api/lineage/normalize", json={"values": {}})
    assert resp.json()["ok"] is False
    assert "图编号" in resp.json()["message"]


# ---------- 快照查询接口 ----------

def test_lineage_list_and_detail():
    normalize("DAG-T-1011", make_topology(version="1", nodes=["JOB_A"], edges=[]))
    listed = client.get("/api/lineage", params={"keyword": "DAG-T-1011"}).json()
    assert listed["total"] == 1
    snap_id = listed["items"][0]["id"]
    detail = client.get(f"/api/lineage/{snap_id}")
    assert detail.status_code == 200
    assert detail.json()["图编号"] == "DAG-T-1011"
    missing = client.get("/api/lineage/9999")
    assert missing.status_code == 404


# ---------- 既有行为与输入输出不变 ----------

def test_existing_module_behaviour_unchanged():
    dag_list = client.get("/api/dag").json()
    assert dag_list["total"] >= 3
    dep_list = client.get("/api/dependency").json()
    assert dep_list["total"] >= 3
    run_list = client.get("/api/runtime").json()
    assert run_list["total"] >= 3

    create = client.post("/api/dag", json={"values": {
        "图编号": "DAG-T-9001", "图名称": "冒烟", "拓扑结构": "占位",
    }}).json()
    assert create["ok"] is True
    action = client.post(
        f"/api/dag/{create['entry']['id']}/actions",
        json={"values": {"action": "提交编排评审"}},
    ).json()
    assert action["ok"] is True
    assert action["entry"]["status"] == "编排评审中"

    dep_detail = client.get("/api/dependency/1")
    assert dep_detail.status_code == 200
    run_detail = client.get("/api/runtime/1")
    assert run_detail.status_code == 200
