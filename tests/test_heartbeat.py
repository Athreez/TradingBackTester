"""Heartbeat pings, against a fake session: nothing here reaches healthchecks.io."""
import time

from paper_trader.heartbeat import Heartbeat


class FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, data.decode()))


def wait_for(session, n):
    end = time.monotonic() + 2
    while len(session.posts) < n and time.monotonic() < end:
        time.sleep(0.01)
    return session.posts


def test_pings_once_a_minute_and_at_once_when_health_changes():
    s = FakeSession()
    hb = Heartbeat("https://hc-ping.com/abc/", every=60, session=s)
    hb.beat()
    hb.beat()  # within the minute: not sent again
    assert wait_for(s, 1) == [("https://hc-ping.com/abc", "ok")]
    hb.beat("no new candles since 10:41")  # failing: sent at once to /fail
    hb.beat("no new candles since 10:41")
    hb.beat()  # recovered: sent at once
    assert sorted(wait_for(s, 3)[1:]) == [("https://hc-ping.com/abc", "ok"),  # each ping is its own thread
                                          ("https://hc-ping.com/abc/fail", "no new candles since 10:41")]
    time.sleep(0.05)
    assert len(s.posts) == 3
