"""从应用元数据构建能力关系，提供可解释的分类与相似应用推荐。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

# 平台业务分类规则；不执行智能体，也不冒充供应方返回的能力图谱。
CATEGORY_TERMS = {
    "文档创作": ("写作", "文档", "报告", "摘要", "ppt", "演示文稿", "writing", "document"),
    "会议办公": ("会议", "纪要", "录音", "日程", "邮件", "流程图", "meeting", "schedule"),
    "数据处理": ("数据", "报表", "分析", "统计", "简历", "筛选", "data", "analysis"),
    "翻译沟通": ("翻译", "多语言", "同传", "口译", "translate", "translation"),
    "法律合规": ("合同", "法律", "合规", "法务", "条款", "legal", "compliance"),
}


def normalized(value: object) -> str:
    """统一元数据的大小写和连续空白，供规则匹配使用。"""
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def analyze(data: dict) -> dict:
    """只基于实际填写内容分析；没有依据或最高分并列时不强行指定分类。"""
    signals = [
        (data.get("name", ""), 3),
        (data.get("summary", data.get("description", "")), 1),
        *[(value, 4) for value in data.get("capabilities", []) if isinstance(value, str)],
        *[(value, 2) for value in data.get("tags", []) if isinstance(value, str)],
    ]
    scores, matches = {}, {}
    for category, terms in CATEGORY_TERMS.items():
        hits = {term for text, _ in signals for term in terms if term in normalized(text)}
        scores[category] = sum(
            weight for text, weight in signals if any(term in normalized(text) for term in terms)
        )
        matches[category] = sorted(hits)
    ranked = sorted(scores, key=lambda category: scores[category], reverse=True)
    category = ranked[0] if scores[ranked[0]] > scores[ranked[1]] else None
    nodes = [{"id": "application", "kind": "application", "label": data.get("name") or "当前应用"}]
    edges, seen = [], set()

    def add(kind: object, value: object, relation: str) -> None:
        """忽略无效或重复信号，将有效元数据加入能力关系图。"""
        if not isinstance(kind, str) or not isinstance(value, str) or not value.strip():
            return
        key = kind + ":" + normalized(value)
        if key in seen:
            return
        seen.add(key)
        node_id = hashlib.sha256(key.encode()).hexdigest()[:20]
        nodes.append({"id": node_id, "kind": kind, "label": value})
        edges.append({"source": "application", "target": node_id, "relation": relation})

    for capability in data.get("capabilities", []):
        add("capability", capability, "provides")
    for tag in data.get("tags", []):
        if tag not in ("自研应用", "第三方接入", data.get("category")):
            add("keyword", tag, "describes")
    add("model", data.get("baseModel"), "uses")
    for dependency in data.get("dependencies", []):
        if isinstance(dependency, dict):
            add(dependency.get("kind", "resource"), dependency.get("resourceId"), "depends_on")
    if category:
        add("category", category, "classified_as")
    return {
        "method": "metadata_rules_v1",
        "suggestedCategory": category,
        "reasons": matches[category] if category else [],
        "graph": {"nodes": nodes, "edges": edges},
    }


def related(source: dict, candidates: list[dict], limit: int = 4) -> list[dict]:
    """按有区分度的共同能力节点推荐相似应用，最多返回 limit 条。"""
    source_graph = analyze(source)["graph"]
    source_nodes = {
        node["id"]: node for node in source_graph["nodes"] if node["kind"] != "application"
    }
    results = []
    weights = {"capability": 5, "keyword": 3, "category": 2, "model": 1}
    candidate_graphs = [
        (item, analyze(item)["graph"]) for item in candidates if item["id"] != source["id"]
    ]
    # 几乎所有应用都有的标签（如“多轮交互”）不能成为推荐任意应用的依据。
    frequency = Counter(
        node["id"]
        for graph in [source_graph, *[g for _, g in candidate_graphs]]
        for node in graph["nodes"]
        if node["kind"] != "application"
    )
    total = len(candidate_graphs) + 1
    for candidate, graph in candidate_graphs:
        shared = [
            node
            for node in graph["nodes"]
            if node["id"] in source_nodes
            and not (total >= 5 and frequency[node["id"]] / total >= 0.8)
        ]
        if not shared:
            continue
        shared.sort(key=lambda node: weights.get(node["kind"], 2), reverse=True)
        results.append(
            {
                "id": candidate["id"],
                "name": candidate["name"],
                "category": candidate["category"],
                "score": sum(weights.get(node["kind"], 2) for node in shared),
                "reasons": [node["label"] for node in shared[:3]],
            }
        )
    return sorted(results, key=lambda item: (-item["score"], item["id"]))[:limit]
