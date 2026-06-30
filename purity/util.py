"""通用文本工具。"""


def norm(text: str) -> str:
    """归一化：转小写并去首尾空白。"""
    return (text or "").lower().strip()


def contains_any(text: str, needles) -> bool:
    """text 中是否包含任意一个 needle（大小写不敏感）。"""
    t = norm(text)
    return any(n.lower() in t for n in needles)


def clip(text: str, n: int = 60) -> str:
    """把文本压成单行并截断到 n 个字符，空文本返回 "(空)"。"""
    text = (text or "").replace("\n", " ").strip()
    return (text[:n] + "…") if len(text) > n else (text or "(空)")


# 兼容旧的私有命名
_norm = norm
_clip = clip
