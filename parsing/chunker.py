"""按标题切块：500-800 字，重叠 100 字，保留标题上下文。"""

MAX_LEN = 800
MIN_LEN = 500
OVERLAP = 100


def chunk_paragraphs(paragraphs: list[dict]) -> list[dict]:
    """输入 [{"level","text"}]，输出 [{"seq","heading","content"}]。"""
    chunks = []
    current_heading = None
    buf_lines: list[str] = []
    buf_len = 0

    def flush():
        nonlocal buf_lines, buf_len
        text = "\n".join(buf_lines).strip()
        if text:
            chunks.append({
                "seq": len(chunks) + 1,
                "heading": current_heading,
                "content": text,
            })
        buf_lines, buf_len = [], 0

    for para in paragraphs:
        if para["level"] > 0:
            if buf_len >= MIN_LEN:
                flush()
            current_heading = para["text"][:280]
            continue
        text = para["text"]
        if buf_len + len(text) > MAX_LEN and buf_len >= MIN_LEN:
            flush()
            # 重叠：保留上一块尾部 OVERLAP 字作为上下文
            prev = chunks[-1]["content"] if chunks else ""
            if prev:
                tail = prev[-OVERLAP:]
                buf_lines = [tail]
                buf_len = len(tail)
        buf_lines.append(text)
        buf_len += len(text)
    flush()
    return chunks
