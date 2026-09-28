import time

import pytest
from ultrademo_protocol import WebhookTopic, webhooks


def test_sign_and_verify_round_trip():
    secret = webhooks.new_secret()
    body = b'{"type":"session.ended"}'
    h = webhooks.headers(secret, "msg_1", body)
    assert h["webhook-signature"].startswith("v1,")
    assert webhooks.verify(secret, h, body)
    # Header names are case-insensitive, as they are over HTTP.
    assert webhooks.verify(secret, {k.upper(): v for k, v in h.items()}, body)
    assert not webhooks.verify(secret, h, body + b" ")
    assert not webhooks.verify(webhooks.new_secret(), h, body)
    assert not webhooks.verify(secret, {**h, "webhook-id": "msg_2"}, body)
    assert not webhooks.verify(secret, {}, body)


def test_known_vector():
    # From the Standard Webhooks reference implementation's test suite.
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    body = b'{"test": 2432232314}'
    sig = webhooks.sign(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, body)
    assert sig == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_stale_timestamps_and_rotation():
    secret, old = webhooks.new_secret(), webhooks.new_secret()
    body = b"{}"
    ts = int(time.time()) - 3600
    h = webhooks.headers(secret, "m", body, timestamp=ts)
    assert not webhooks.verify(secret, h, body)
    assert webhooks.verify(secret, h, body, now=ts + 10)
    # During a rotation the header carries both signatures; either key verifies.
    h = webhooks.headers(secret, "m", body)
    h["webhook-signature"] += " " + webhooks.sign(old, "m", int(h["webhook-timestamp"]), body)
    assert webhooks.verify(secret, h, body) and webhooks.verify(old, h, body)


def test_secret_format_and_topics():
    with pytest.raises(ValueError):
        webhooks.sign("not-a-secret", "m", 0, b"")
    assert WebhookTopic("session.ended") is WebhookTopic.SESSION_ENDED
