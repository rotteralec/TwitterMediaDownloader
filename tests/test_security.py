"""Regression tests for the security fixes.

These lock in behavior that was previously wrong (or that a future edit could
quietly break):
  * ``_is_allowed_cdn`` — the SSRF/open-proxy allowlist. A look-alike host like
    ``faketwimg.com`` must be REJECTED (the original substring filter let it through).
  * ``_strip_port`` — pulling the bare IP out of an ``X-Forwarded-For`` element
    that may carry a ``:port`` (App Service sends ``ip:port``).
  * ``RateLimiter`` — the per-IP daily limit still works, and the tracking dict is
    hard-bounded so a flood of distinct IPs can't exhaust memory.

Only our own logic is tested here (not FastAPI / yt-dlp), so these stay fast and
stable across dependency upgrades.
"""

from app.main import _is_allowed_cdn, _strip_port
from app.rate_limit import RateLimiter


# --- #1 SSRF allowlist -------------------------------------------------------

def test_allowed_cdn_accepts_real_twimg():
    assert _is_allowed_cdn("https://video.twimg.com/ext_tw_video/1/v.mp4") is True
    assert _is_allowed_cdn("https://pbs.twimg.com/media/abc.jpg") is True
    assert _is_allowed_cdn("https://twimg.com/x") is True  # apex domain


def test_allowed_cdn_rejects_lookalikes_and_bad_schemes():
    # The exact bypasses that defeated the old substring filter:
    assert _is_allowed_cdn("https://faketwimg.com/x.mp4") is False
    assert _is_allowed_cdn("https://eviltwimg.com/x.mp4") is False
    # Domain-suffix confusion:
    assert _is_allowed_cdn("https://twimg.com.attacker.com/x") is False
    # Unrelated host:
    assert _is_allowed_cdn("https://evil.com/x") is False
    # Right host but wrong scheme -> must be https:
    assert _is_allowed_cdn("http://video.twimg.com/x.mp4") is False


# --- #2 X-Forwarded-For port stripping ---------------------------------------

def test_strip_port_ipv4():
    assert _strip_port("68.60.78.17:62309") == "68.60.78.17"
    assert _strip_port("68.60.78.17") == "68.60.78.17"


def test_strip_port_ipv6():
    assert _strip_port("[2001:db8::1]:443") == "2001:db8::1"
    # Bare IPv6 (multiple colons, no port) must be left intact:
    assert _strip_port("2001:db8::1") == "2001:db8::1"


def test_strip_port_trims_whitespace():
    assert _strip_port("  68.60.78.17:9 ") == "68.60.78.17"


# --- #2 rate limiter: still limits, and memory is bounded --------------------

def test_limiter_enforces_daily_limit():
    rl = RateLimiter(daily_limit=2, max_entries=100)
    assert rl.check("1.1.1.1") == (True, 1)   # 1st allowed, 1 left
    assert rl.check("1.1.1.1") == (True, 0)   # 2nd allowed, 0 left
    allowed, remaining = rl.check("1.1.1.1")  # 3rd blocked
    assert allowed is False
    assert remaining == 0


def test_limiter_memory_is_bounded():
    rl = RateLimiter(daily_limit=3, max_entries=100)
    # Simulate a flood of 1000 distinct IPs (the memory-DoS scenario).
    for i in range(1000):
        rl.check(f"10.0.{i // 256}.{i % 256}")
    # The tracking dict must never exceed the cap.
    assert len(rl._counts) <= 100
