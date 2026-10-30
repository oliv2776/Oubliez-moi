from app.login_security import LoginAttemptLimiter


def test_login_attempt_limiter_blocks_after_five_attempts_and_expires():
    now = [100.0]
    limiter = LoginAttemptLimiter(max_attempts=5, window_seconds=900, clock=lambda: now[0])

    assert [limiter.consume("192.0.2.10") for _ in range(5)] == [0, 0, 0, 0, 0]
    assert limiter.consume("192.0.2.10") == 900
    assert limiter.consume("192.0.2.11") == 0  # une autre IP n'est pas bloquée
    now[0] += 901
    assert limiter.consume("192.0.2.10") == 0


def test_login_attempt_limiter_reset_after_success():
    limiter = LoginAttemptLimiter(max_attempts=2, window_seconds=60, clock=lambda: 10.0)
    assert limiter.consume("198.51.100.2") == 0
    assert limiter.consume("198.51.100.2") == 0
    assert limiter.consume("198.51.100.2") > 0
    limiter.reset("198.51.100.2")
    assert limiter.consume("198.51.100.2") == 0


def test_login_attempt_limiter_bounds_client_state():
    limiter = LoginAttemptLimiter(max_attempts=2, window_seconds=60, clock=lambda: 10.0, max_clients=2)
    for client in ("a", "b", "c"):
        assert limiter.consume(client) == 0
    assert len(limiter._attempts) == 2
