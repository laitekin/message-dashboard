from aftersales_events.preprocess import extract_entities, mask, parse_customer_uid, preprocess
from aftersales_events.segment import segment


def test_parse_customer_uid():
    assert parse_customer_uid("「内部」示例团队-1380000001") == "1380000001"
    assert parse_customer_uid("示例客户&支持团队-1380000002<4-000000-0-0>") == "1380000002"
    assert parse_customer_uid("没有编号的群") is None


def test_mask():
    s = mask("电话 13812345678 key sk-abcdefghijklmnopqrstuv api_key: xyz123 邮箱 test@example.com")
    assert "13812345678" not in s and "<PHONE>" in s
    assert "sk-abcdefghijklmnopqrstuv" not in s
    assert "xyz123" not in s
    assert "<EMAIL>" in s


def test_entities():
    e = extract_entities("claude-4.6-opus 报错 request_id=chatptmsg-abc123def456 需要 500万 TPM 渠道 newapi-ss-demo-01 @李四")
    assert "claude-4.6-opus" in e["models"]
    assert "chatptmsg-abc123def456" in e["request_ids"]
    assert any("TPM" in q for q in e["quantities"])
    assert "newapi-ss-demo-01" in e["channels"]
    assert "李四" in e["mentions"]


def test_preprocess_dedup_noise_image(raw, settings):
    msgs, stats = preprocess(raw, settings.enabled_types, settings.section("preprocess"))
    ids = [m.id for m in msgs]
    assert "2" not in ids  # OCR 重复
    assert "3" not in ids and "21" not in ids  # 噪声：收到 / 👍
    assert stats["duplicate"] == 1 and stats["noise"] == 2
    img = next(m for m in msgs if m.id == "5")
    assert img.is_image
    m1 = next(m for m in msgs if m.id == "1")
    assert m1.role == "客户" and m1.customer_uid == "1380000001"
    assert "故障" in m1.hints
    m7 = next(m for m in msgs if m.id == "7")
    assert "13812345678" not in m7.content


def test_segment_gap(raw, settings):
    msgs, _ = preprocess(raw, settings.enabled_types, settings.section("preprocess"))
    windows = segment(msgs, settings.section("segment"))
    g1 = [w for w in windows if w.group_number == "R:1001"]
    # 10:00-10:12 与 15:00 之间间隔 > 30 分钟 → 两个窗口
    assert len(g1) == 2
    assert [m.id for m in g1[1].messages] == ["8", "9"]


def test_segment_max_messages_overlap(raw, settings):
    msgs, _ = preprocess(raw, settings.enabled_types, settings.section("preprocess"))
    cfg = {**settings.section("segment"), "max_messages": 3, "overlap": 1}
    windows = segment([m for m in msgs if m.group_number == "R:1001"], cfg)
    first, second = windows[0], windows[1]
    assert len(first.messages) == 3
    assert second.messages[0].id == first.messages[-1].id  # 重叠
