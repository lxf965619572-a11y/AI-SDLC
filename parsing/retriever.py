"""BM25 检索（jieba 分词 + rank_bm25），纯 Python 无需 GPU。"""
import jieba
from rank_bm25 import BM25Okapi


class BM25Retriever:
    def __init__(self, texts: list[str]):
        self.texts = texts
        self.tokenized = [list(jieba.cut(t)) for t in texts]
        self.index = BM25Okapi(self.tokenized) if texts else None

    def top_k(self, query: str, k: int = 5) -> list[tuple[int, str, float]]:
        """返回 [(原始下标, 文本, 分数)]。"""
        if not self.index:
            return []
        scores = self.index.get_scores(list(jieba.cut(query)))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [(i, self.texts[i], float(scores[i])) for i in ranked[:k] if scores[i] > 0]
