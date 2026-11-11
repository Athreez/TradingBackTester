import io
import logging

from core.logsafe import RedactingFormatter, protect_handlers, redact

BOT_TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"


def test_redacts_telegram_token_in_url():
    msg = f"HTTPSConnectionPool(host='api.telegram.org'): Max retries with url: /bot{BOT_TOKEN}/getUpdates"
    out = redact(msg)
    assert BOT_TOKEN not in out and "/bot***/getUpdates" in out


def test_redacts_jwt_and_env_secrets(monkeypatch):
    monkeypatch.setenv("INDMONEY_MPIN", "482913")
    jwt = ".".join(["eyJ" + "fakeheader", "fakepayload", "fakesignature"])  # built here: not a real token
    out = redact(f"auth {jwt} mpin 482913 strike 24000")
    assert jwt not in out and "482913" not in out and "strike 24000" in out


def test_handlers_mask_messages_and_tracebacks():
    stream = io.StringIO()
    logger = logging.getLogger("test_logsafe")
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    protect_handlers(logger)
    assert isinstance(handler.formatter, RedactingFormatter)
    try:
        raise ConnectionError(f"url: /bot{BOT_TOKEN}/sendMessage")
    except ConnectionError as e:
        logger.warning("telegram send failed: %s", e)
        logger.exception("with traceback")
    logger.removeHandler(handler)
    assert BOT_TOKEN not in stream.getvalue() and "/bot***/sendMessage" in stream.getvalue()
