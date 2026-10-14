from monitor.problems import problem_notice
from monitor.tonghuashun import _encrypt, format_selfstock, parse_selfstock


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
