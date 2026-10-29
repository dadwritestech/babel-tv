from babel_tv.playlist import Channel, ffmpeg_input_args, list_url, parse_m3u, search

SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="AnimaxAsia.sg@India" tvg-logo="https://x/a.png" group-title="Animation",Animax Asia India (1080p) [Geo-blocked]
https://example.com/animax.m3u8
#EXTINF:-1 tvg-id="ABPNews.in@SD" group-title="News",ABP News (1080p)
https://example.com/abp.m3u8
#EXTINF:-1 tvg-id="CGNTVJapan.jp@SD" http-referrer="http://japan.cgntv.net/p.html" group-title="Religious",CGNTV Japan (1080p)
#EXTVLCOPT:http-referrer=http://japan.cgntv.net/p.html
#EXTVLCOPT:http-user-agent=Mozilla/5.0
https://example.com/cgn.m3u8
"""


def test_parse_m3u():
    chans = parse_m3u(SAMPLE)
    assert [c.title for c in chans] == ["Animax Asia India", "ABP News", "CGNTV Japan"]
    assert chans[0].tags == ["Geo-blocked"]
    assert chans[1].group == "News" and chans[1].quality == "1080p"
    assert chans[2].referrer == "http://japan.cgntv.net/p.html"
    assert chans[2].user_agent == "Mozilla/5.0"


def test_search_skips_geo_blocked_by_default():
    chans = parse_m3u(SAMPLE)
    assert [c.title for c in search(chans)] == ["ABP News", "CGNTV Japan"]
    assert len(search(chans, include_geo_blocked=True)) == 3
    assert [c.title for c in search(chans, group="news")] == ["ABP News"]
    assert [c.title for c in search(chans, query="abp")] == ["ABP News"]


def test_list_urls():
    assert list_url(language="te").endswith("/languages/tel.m3u")
    assert list_url(country="IN").endswith("/countries/in.m3u")
    assert list_url().endswith("/index.m3u")


def test_ffmpeg_headers():
    ch = Channel(name="x", url="u", referrer="http://r/", user_agent="UA")
    args = ffmpeg_input_args(ch)
    assert args[args.index("-user_agent") + 1] == "UA"
    assert args[args.index("-headers") + 1] == "Referer: http://r/\r\n"


def test_comma_inside_quoted_attribute():
    text = ('#EXTINF:-1 tvg-id="X.in" http-user-agent="Mozilla/5.0 (KHTML, like Gecko)" '
            'group-title="News",Some News (576p)\nhttps://example.com/x.m3u8\n')
    (c,) = parse_m3u(text)
    assert c.title == "Some News"
    assert c.user_agent == "Mozilla/5.0 (KHTML, like Gecko)"
