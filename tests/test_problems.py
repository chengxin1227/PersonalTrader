from datetime import datetime, timedelta, timezone

from monitor.problems import ProblemLatch, problem_notice


def test_rate_limit_and_quota_are_notices():
    limit = problem_notice(
        "Moomoo after-hours rank failed: Get US After-Hours Rank request failed "
        "due to high frequency. Maximum 60 times per 30 seconds."
    )
    assert limit is not None
    assert limit[0] == "limit:after-hours-rank"
    assert limit[1] == "OpenD 超限"
    assert "盘后涨幅榜" in limit[2]
    assert "60 times per 30 seconds" in limit[2]

    quota = problem_notice(
        "Insufficient historical K-line quota. Request failed (stock: 100/100, option: 0/20)."
    )
    assert quota is not None
    assert quota[0] == "quota:kline"
    assert quota[1] == "OpenD 额度用完"
    assert "100/100" in quota[2]


def test_disconnects_are_left_to_the_opend_alert():
    assert problem_notice("Moomoo after-hours rank failed: Network interruption.") is None
    assert problem_notice("Moomoo quote context connect timeout") is None


def test_other_errors_notify_once_until_the_problem_goes_quiet():
    notice = problem_notice("Trend scan failed: unexpected payload")
    assert notice is not None
    assert notice[1] == "扫描出错"
    latch = ProblemLatch(quiet_seconds=120)
    now = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    assert latch.should_send(notice[0], now)
    assert not latch.should_send(notice[0], now + timedelta(seconds=5))
    assert latch.should_send(notice[0], now + timedelta(seconds=5 + 121))
