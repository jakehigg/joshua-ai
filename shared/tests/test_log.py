import json
import logging

from joshua_shared import log


def test_configure_emits_json_shape(capsys) -> None:
    logger = log.configure("INFO", "joshua-shared")
    logger.info("hello")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert set(record) >= {"ts", "level", "service", "logger", "message"}
    assert record["level"] == "INFO"
    assert record["service"] == "joshua-shared"
    assert record["message"] == "hello"


def test_dict_message_flattens_fields(capsys) -> None:
    logger = log.configure("INFO", "svc")
    logger.info({"message": "did a thing", "status": 200, "duration_ms": 4.2})
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["message"] == "did a thing"
    assert record["status"] == 200
    assert record["duration_ms"] == 4.2


def test_redact_masks_bearer_and_credentials() -> None:
    assert "secret" not in log.redact("Authorization: Bearer secret-value")
    assert "[REDACTED]" in log.redact("Authorization: Bearer secret-value")
    for cred in ("sk-ant-abc123", "xoxb-1-2-3", "ak2_zzz", "GOCSPX-yyy"):
        assert cred not in log.redact(f"token={cred} end")
        assert "[REDACTED]" in log.redact(f"token={cred} end")


def test_logs_never_contain_a_raw_bearer(capsys) -> None:
    logger = log.configure("INFO", "svc")
    logger.info("calling with Authorization: Bearer super-secret-token")
    out = capsys.readouterr().out
    assert "super-secret-token" not in out
    assert "[REDACTED]" in out


def test_redaction_applies_to_dict_field_values(capsys) -> None:
    logger = log.configure("INFO", "svc")
    logger.info({"message": "ok", "header": "Bearer leak-me"})
    out = capsys.readouterr().out
    assert "leak-me" not in out


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_healthcheck_filter_drops_probes() -> None:
    f = log.HealthCheckFilter()
    assert f.filter(_access_record("/healthz")) is False
    assert f.filter(_access_record("/readyz?x=1")) is False
    assert f.filter(_access_record("/v1/turns")) is True


def test_install_healthcheck_filter_is_idempotent() -> None:
    access = logging.getLogger("uvicorn.access")
    access.filters = [f for f in access.filters if not isinstance(f, log.HealthCheckFilter)]
    log.install_healthcheck_filter()
    log.install_healthcheck_filter()
    count = sum(isinstance(f, log.HealthCheckFilter) for f in access.filters)
    assert count == 1


def test_configure_from_env_defaults_to_info(monkeypatch, capsys) -> None:
    monkeypatch.delenv(log.LEVEL_ENV, raising=False)
    log.configure_from_env("joshua-test")
    logging.getLogger("x").info({"message": "hello"})
    record = json.loads(capsys.readouterr().out.strip())
    assert record["level"] == "INFO"
    assert record["service"] == "joshua-test"


def test_configure_from_env_reads_the_level(monkeypatch, capsys) -> None:
    monkeypatch.setenv(log.LEVEL_ENV, "WARNING")
    log.configure_from_env("joshua-test")
    logging.getLogger("x").info({"message": "dropped"})
    logging.getLogger("x").warning({"message": "kept"})
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "kept"
