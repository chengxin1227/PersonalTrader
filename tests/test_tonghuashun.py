from monitor.config import Settings
from monitor.engine import _shared_rule_group_names
from monitor.problems import problem_notice
from monitor.tonghuashun import (
    TonghuashunClient,
    _encrypt,
    format_selfstock,
    parse_selfstock,
    previous_watchlist_session,
    summary_group_order,
)


def test_important_watchlist_previous_session_cycle():
    assert previous_watchlist_session("盘前") == "盘后"
    assert previous_watchlist_session("盘中") == "盘前"
    assert previous_watchlist_session("盘后") == "盘中"
    assert previous_watchlist_session("其他") is None


def test_shared_rule_group_names_from_live_rules():
    from monitor.config import ROOT
    from monitor.rules import load_rules

    shared = set(_shared_rule_group_names(load_rules(ROOT / "config" / "rules.yaml")))
    assert "后半段1分钟涨超10%的股票" in shared
    assert "盘中收盘涨超25%后续涨超10%的股票" in shared
    assert "5日曾涨超50%的股票" in shared
    # Retired singles: 我的自选 only — not shared rule groups.
    assert "5日曾涨超50%后续超5%或曾涨超100%后续超0%的股票" not in shared
    assert "盘中熔断或涨超50%后续涨超5%的股票" not in shared
    assert "盘后涨幅最高前5的股票" not in shared


def test_rule_group_session_tags_round_trip(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        ths_username="",
        ths_password="",
        slack_webhook_url="https://example.invalid",
    )
    client = TonghuashunClient(settings)
    client._tag_rule_group_symbols(
        "后半段1分钟涨超10%的股票", ["AAA", "BBB"], "盘前", members=["AAA", "BBB"]
    )
    client._tag_rule_group_symbols(
        "后半段1分钟涨超10%的股票", ["CCC"], "盘中", members=["AAA", "BBB", "CCC"]
    )
    tags = client._rule_group_tags()["后半段1分钟涨超10%的股票"]
    assert tags == {"AAA": "盘前", "BBB": "盘前", "CCC": "盘中"}

    # Simulate clear of previous session tags without HTTP by pruning like the clear path.
    removed = [symbol for symbol, tagged in list(tags.items()) if tagged == "盘前"]
    for symbol in removed:
        del tags[symbol]
    client._save_rule_group_tags()
    assert client._rule_group_tags()["后半段1分钟涨超10%的股票"] == {"CCC": "盘中"}


def test_summary_bump_moves_repeats_to_top():
    existing = ["IMC", "OMH", "TANH", "BRIA"]
    assert summary_group_order(existing, ["TANH"], bump=True) == [
        "TANH",
        "IMC",
        "OMH",
        "BRIA",
    ]


def test_important_afterhours_rank_order():
    members = ["LOW", "HIGH", "MID", "NONE"]
    changes = {"HIGH": 40.0, "MID": 12.0, "LOW": 3.0}
    ordered = sorted(
        members,
        key=lambda symbol: changes.get(symbol, float("-inf")),
        reverse=True,
    )
    assert ordered == ["HIGH", "MID", "LOW", "NONE"]


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
