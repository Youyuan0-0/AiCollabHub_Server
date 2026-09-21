"""真实召回实现：本地 BM25、远程向量嵌入及重排序，禁止静默返回演示结果。"""

import math
import re
from collections import Counter

import httpx
from fastapi import HTTPException

from app.core.config import Settings


def tokens(text: str) -> list[str]:
    """英文按词、中文按单字及相邻双字切词，让未安装分词器时也可检索中文。"""
    terms = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold())
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.extend(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return terms


def keyword_scores(query: str, texts: list[str]) -> list[float]:
    """计算 BM25 并压缩到 0–1；不相关文本为零分，分数不是语义相似概率。"""
    if not texts:
        return []
    documents = [Counter(tokens(text)) for text in texts]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths) or 1
    terms = set(tokens(query))
    frequencies = {term: sum(term in document for document in documents) for term in terms}
    scores = []
    for document, length in zip(documents, lengths, strict=True):
        score = 0.0
        for term in terms:
            count = document[term]
            inverse = math.log(
                1 + (len(texts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
            )
            score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
        scores.append(score / (1 + score))
    return scores


def cosine(left: list[float], right: list[float]) -> float:
    """返回非负余弦相似度；维度变化要求重新构建索引，不能截断向量蒙混处理。"""
    if len(left) != len(right):
        raise HTTPException(503, "向量维度发生变化，请重新构建索引")
    denominator = math.sqrt(
        sum(value * value for value in left) * sum(value * value for value in right)
    )
    return (
        max(0.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True)) / denominator))
        if denominator
        else 0.0
    )


class RetrievalProvider:
    """通过部署者指定的完整端点调用模型；地址和密钥不由普通用户提交。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _post(self, endpoint: str | None, payload: dict) -> dict:
        if not endpoint:
            raise HTTPException(503, "模型服务尚未配置，请联系管理员或使用经济模式全文检索")
        headers = {}
        if self.settings.knowledge_model_key:
            headers["Authorization"] = (
                "Bearer " + self.settings.knowledge_model_key.get_secret_value()
            )
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "知识库模型服务调用失败，请稍后重试") from None

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """按 32 段分批调用 OpenAI 兼容 embeddings 端点，并验证数量、顺序及有限数值。"""
        vectors = []
        endpoint = (
            str(self.settings.knowledge_embedding_url)
            if self.settings.knowledge_embedding_url
            else None
        )
        for offset in range(0, len(texts), 32):
            batch = texts[offset : offset + 32]
            result = self._post(endpoint, {"model": model, "input": batch})
            try:
                items = sorted(result["data"], key=lambda item: item["index"])
                if [item["index"] for item in items] != list(range(len(batch))):
                    raise ValueError
                for item in items:
                    vector = item["embedding"]
                    if (
                        not isinstance(vector, list)
                        or not 1 <= len(vector) <= 8192
                        or any(
                            type(value) not in (int, float) or not math.isfinite(value)
                            for value in vector
                        )
                        or not any(vector)
                        or (vectors and len(vector) != len(vectors[0]))
                    ):
                        raise ValueError
                    vectors.append(vector)
            except (KeyError, TypeError, ValueError):
                raise HTTPException(503, "向量服务返回的数据无效") from None
        return vectors

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """调用兼容 /rerank 协议的端点；必须返回所有候选项及 0–1 分数。"""
        endpoint = (
            str(self.settings.knowledge_rerank_url) if self.settings.knowledge_rerank_url else None
        )
        result = self._post(
            endpoint,
            {
                "model": self.settings.knowledge_rerank_model,
                "query": query,
                "documents": texts,
                "top_n": len(texts),
            },
        )
        try:
            items = sorted(result["results"], key=lambda item: item["index"])
            if [item["index"] for item in items] != list(range(len(texts))):
                raise ValueError
            scores = [item["relevance_score"] for item in items]
            if any(
                type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
                for score in scores
            ):
                raise ValueError
            return scores
        except (KeyError, TypeError, ValueError):
            raise HTTPException(503, "重排序服务返回的数据无效") from None
