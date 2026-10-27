from monitor.quote_link import page_url, quote_url


def test_quote_url_opens_tonghuashun_app():
    assert quote_url("gygy") == "amihexin://"


def test_page_url_opens_moomoo():
    assert page_url("gygy") == "https://www.moomoo.com/stock/GYGY-US"
