from __future__ import annotations

import hashlib
import re

# Thứ tự có ý nghĩa: pattern dài/cụ thể chạy trước để số thẻ 16 chữ số
# không bị pattern ngắn hơn (CCCD, điện thoại) che mất một phần.
PII_PATTERNS: dict[str, str] = {
    "email": r"[\w\.-]+@[\w\.-]+\.\w+",
    "credit_card": r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
    "cccd": r"\b\d{12}\b",
    "phone_vn": r"(?<!\d)(?:\+84|0)(?:[ .-]?\d){9}(?!\d)",
    # Hộ chiếu Việt Nam: 1 chữ cái in hoa + 7 chữ số, ví dụ B1234567.
    "passport_vn": r"\b[A-Z]\d{7}\b",
}

_COMPILED_PATTERNS = {name: re.compile(pattern) for name, pattern in PII_PATTERNS.items()}


def scrub_text(text: str) -> str:
    safe = text
    for name, pattern in _COMPILED_PATTERNS.items():
        safe = pattern.sub(f"[REDACTED_{name.upper()}]", safe)
    return safe


def summarize_text(text: str, max_len: int = 80) -> str:
    safe = scrub_text(text).strip().replace("\n", " ")
    return safe[:max_len] + ("..." if len(safe) > max_len else "")


def hash_user_id(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
