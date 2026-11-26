from monitor.config import Settings
from monitor.engine import _shared_rule_group_names
from monitor.problems import problem_notice
from monitor.tonghuashun import (
    TonghuashunClient,
    _SESSION_WATCHLIST_GROUP,
    _SESSION_WATCHLIST_GROUPS,
    _encrypt,
    _session_watchlist_group,
    format_selfstock,
    parse_selfstock,
    previous_watchlist_session,
    summary_group_order,
)


def test_session_watchlist_groups_are_今日盘x():
    assert _SESSION_WATCHLIST_GROUP == {
        "盘前": "今日盘前分组",
        "盘中": "今日盘中分组",
        "盘后": "今日盘后分组",
    }
    assert _session_watchlist_group("盘后") == "今日盘后分组"
    assert set(_SESSION_WATCHLIST_GROUPS) == {
        "今日盘前分组",
        "今日盘中分组",
        "今日盘后分组",
    }


def test_important_watchlist_previous_session_cycle():
    # Still used for shared rule-group session tags.
    assert previous_watchlist_session("盘前") == "盘后"
    assert previous_watchlist_session("盘中") == "盘前"
    assert previous_watchlist_session("盘后") == "盘中"
    assert previous_watchlist_session("其他") is None


def test_shared_rule_group_names_from_live_rules():
    from monitor.config import ROOT
    from monitor.rules import load_rules

    config = load_rules(ROOT / "config" / "rules.yaml")
    shared = set(_shared_rule_group_names(config))
    # Board digests are not shared singles; AH/PM halt-or-5d use different group names.
    assert "今日盘后涨幅最高前5的股票" not in shared
    assert "熔断或5日盘后前5的股票" not in shared
    assert "熔断或5日盘后的股票" not in shared
    assert "熔断或5日盘前的股票" not in shared
    # Past-5d 100% pool reuses one THS name across 盘前/盘中/盘后.
    assert "过去5日超100%的股票" in shared


def test_sync_groups_keep_includes_session_and_important():
    keep_names = ["今日盘后涨幅最高前5的股票"]
    keep = {
        "今日自选",
        *_SESSION_WATCHLIST_GROUPS,
        *(name.strip() for name in keep_names if name.strip()),
    }
    assert keep == {
        "今日自选",
        "今日盘前分组",
        "今日盘中分组",
        "今日盘后分组",
        "今日盘后涨幅最高前5的股票",
    }


def test_clear_session_day_clears_session_group_and_important_once(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        ths_username="",
        ths_password="",
        slack_webhook_url="https://example.invalid",
    )
    client = TonghuashunClient(settings)
    client.set_important_group("今日自选")
    cleared_batches: list[list[str]] = []

    def fake_clear_day(names: list[str], day: str) -> list[str]:
        cleared_batches.append(list(names))
        return list(names)

    client._clear_day_sync = fake_clear_day  # type: ignore[method-assign]
    client._clear_session_watchlist_tags = lambda session: None  # type: ignore[method-assign]

    first = client._clear_session_day_sync(
        "盘后", "2026-11-20", ["今日盘后涨幅最高前5的股票"]
    )
    assert "今日盘后分组" in first
    assert "今日自选" in first
    assert any("今日盘后分组" in batch for batch in cleared_batches)
    assert any("今日自选" in batch for batch in cleared_batches)

    # Same ET day: 今日自选 already stamped, so important clear is a no-op.
    client._group_days = lambda: {"今日自选": "2026-11-20"}  # type: ignore[method-assign]
    cleared_batches.clear()
    second = client._clear_session_day_sync(
        "盘中", "2026-11-20", ["某盘中规则"]
    )
    assert "今日盘中分组" in second
    assert "今日自选" not in second


def test_rule_group_session_tags_round_trip(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        ths_username="",
        ths_password="",
        slack_webhook_url="https://example.invalid",
    )
    client = TonghuashunClient(settings)
    client._tag_rule_group_symbols(
        "后半段5分钟涨超10%的股票", ["AAA", "BBB"], "盘前", members=["AAA", "BBB"]
    )
    client._tag_rule_group_symbols(
        "后半段5分钟涨超10%的股票", ["CCC"], "盘中", members=["AAA", "BBB", "CCC"]
    )
    tags = client._rule_group_tags()["后半段5分钟涨超10%的股票"]
    assert tags == {"AAA": "盘前", "BBB": "盘前", "CCC": "盘中"}

    # Simulate clear of previous session tags without HTTP by pruning like the clear path.
    removed = [symbol for symbol, tagged in list(tags.items()) if tagged == "盘前"]
    for symbol in removed:
        del tags[symbol]
    client._save_rule_group_tags()
    assert client._rule_group_tags()["后半段5分钟涨超10%的股票"] == {"CCC": "盘中"}


def test_summary_bump_moves_repeats_to_top():
    existing = ["IMC", "OMH", "TANH", "BRIA"]
    assert summary_group_order(existing, ["TANH"], bump=True) == [
        "TANH",
        "IMC",
        "OMH",
        "BRIA",
    ]


def test_summary_new_alert_also_goes_to_front():
    existing = ["IMC", "OMH"]
    assert summary_group_order(existing, ["AAA"], bump=True) == [
        "AAA",
        "IMC",
        "OMH",
    ]


def test_summary_no_bump_preserves_order_and_appends_new():
    existing = ["TANH", "IMC", "OMH"]
    # Pool refresh must not bury TANH after a repeat bump.
    assert summary_group_order(
        existing, ["CKX", "EDSA", "IMC", "OMH", "TANH"], bump=False
    ) == ["TANH", "IMC", "OMH", "CKX", "EDSA"]


def test_group_content_keeps_the_order_given():
    rows = [("KO", "169"), ("GMEX", "186")]
    assert format_selfstock(rows) == "KO|GMEX,169|186"
    assert parse_selfstock(format_selfstock(rows)) == rows


def test_selfstock_pairs_codes_with_markets():
    assert parse_selfstock("GMEX|AIXI|KO,186|185|169") == [
        ("GMEX", "186"),
        ("AIXI", "185"),
        ("KO", "169"),
    ]


def test_one_line_public_key_encrypts():
    key = (
        "-----BEGIN PUBLIC KEY----- "
        "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf "
        "9Cnzj4p4WGeKLs1Pt8QuKUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQ== "
        "-----END PUBLIC KEY-----"
    )
    assert _encrypt(key, "secret")


def test_empty_selfstock():
    assert parse_selfstock("") == []
    assert parse_selfstock(",") == []


def test_add_failure_is_its_own_notice():
    notice = problem_notice("Tonghuashun add failed: 同花顺登录失败: 密码错误")
    assert notice is not None
    assert notice[0] == "error:tonghuashun"
    assert notice[1] == "加自选失败"
    assert notice[2] == "同花顺加自选失败。"

    named = problem_notice("Tonghuashun add failed for NFT, OMH: 同花顺加自选失败: 网络错误")
    assert named is not None
    assert named[0] == "error:tonghuashun:NFT OMH"
    assert named[1] == "加自选失败 NFT OMH"
    assert "NFT OMH" in named[2]
