from app.pii import hash_user_id, scrub_text, summarize_text


def test_scrub_email() -> None:
    out = scrub_text("Email me at student@vinuni.edu.vn")
    assert "student@" not in out
    assert "REDACTED_EMAIL" in out


def test_scrub_common_vietnamese_phone_formats() -> None:
    phone_numbers = (
        "0901234567",
        "090 123 4567",
        "090.123.4567",
        "090-123-4567",
        "+84 90 123 4567",
    )

    for phone_number in phone_numbers:
        out = scrub_text(f"Contact: {phone_number}")
        assert phone_number not in out
        assert "REDACTED_PHONE_VN" in out


def test_scrub_cccd() -> None:
    out = scrub_text("CCCD của tôi là 001099012345, cảm ơn")
    assert "001099012345" not in out
    assert "REDACTED_CCCD" in out


def test_scrub_credit_card_formats() -> None:
    for card in ("4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111"):
        out = scrub_text(f"Card: {card}")
        assert card not in out
        assert "REDACTED_CREDIT_CARD" in out
        # Số thẻ phải được che trọn vẹn, không bị pattern ngắn hơn cắt một phần.
        assert "1111" not in out


def test_scrub_passport() -> None:
    out = scrub_text("Passport B1234567 expires soon")
    assert "B1234567" not in out
    assert "REDACTED_PASSPORT_VN" in out


def test_scrub_multiple_pii_in_one_message() -> None:
    raw = "a@b.vn, 0987654321, 079203001234, 5500 0000 0000 0004"
    out = scrub_text(raw)
    for fragment in ("a@b.vn", "0987654321", "079203001234", "5500 0000 0000 0004"):
        assert fragment not in out


def test_scrub_keeps_operational_identifiers() -> None:
    text = "req-1a2b3c4d latency 2500ms model claude-sonnet-4-5 session s01"
    assert scrub_text(text) == text


def test_summarize_text_scrubs_before_truncating() -> None:
    out = summarize_text("Phone 0987654321 " + "x" * 200, max_len=40)
    assert "0987654321" not in out
    assert out.endswith("...")


def test_hash_user_id_is_stable_and_not_reversible_text() -> None:
    assert hash_user_id("u01") == hash_user_id("u01")
    assert hash_user_id("u01") != "u01"
    assert len(hash_user_id("u01")) == 12
