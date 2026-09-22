from reovault.web.ratelimit import LoginLimiter


def test_allows_attempts_under_the_threshold():
    limiter = LoginLimiter(max_attempts=5, window_secs=900, lockout_secs=60)
    for _ in range(4):
        assert limiter.retry_after("1.2.3.4") is None
        limiter.record_failure("1.2.3.4")


def test_blocks_after_max_attempts():
    limiter = LoginLimiter(max_attempts=5, window_secs=900, lockout_secs=60)
    for _ in range(5):
        limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is not None
    assert limiter.retry_after("1.2.3.4") > 0


def test_success_clears_the_bucket():
    limiter = LoginLimiter(max_attempts=5, window_secs=900, lockout_secs=60)
    for _ in range(5):
        limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is not None
    limiter.record_success("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is None


def test_global_bucket_blocks_even_a_fresh_key():
    """If forwarded_allow_ips is misconfigured, every request could collapse
    onto one proxy IP; the global bucket must still limit correctly. It uses
    a separate (looser) threshold from the per-key one, set explicitly here."""
    limiter = LoginLimiter(max_attempts=3, global_max_attempts=3, window_secs=900, lockout_secs=60)
    for _ in range(3):
        limiter.record_failure("attacker")
    assert limiter.retry_after("victim") is not None


def test_a_users_own_success_is_not_blocked_by_their_own_prior_failures_in_the_global_bucket():
    """The common single-user case: the global bucket's looser default
    threshold must not re-punish a user who just typo'd their own password
    a few times and then got it right."""
    limiter = LoginLimiter(max_attempts=5, window_secs=900, lockout_secs=60)
    for _ in range(5):
        limiter.record_failure("1.2.3.4")
    limiter.record_success("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is None


def test_max_keys_bound_evicts_oldest():
    limiter = LoginLimiter(max_attempts=100, window_secs=900, lockout_secs=60, max_keys=3)
    for i in range(10):
        limiter.record_failure(f"key{i}")
    # Should not grow unboundedly (accounts for the __global__ key too).
    assert len(limiter._failures) <= 4
