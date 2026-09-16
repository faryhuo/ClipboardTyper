from clipboard_typer.core.logger import close_logger, make_logger


def test_shutdown_drains_background_logs_and_keeps_traceback(tmp_path):
    path = tmp_path / "app.log"
    logger = make_logger(path)
    try:
        logger.info("Started")
        try:
            raise RuntimeError("test failure")
        except RuntimeError:
            logger.exception("Save failed")
    finally:
        close_logger(logger)
    content = path.read_text(encoding="utf-8")
    assert "Started" in content
    assert "Traceback" in content
    assert "RuntimeError: test failure" in content
    assert not logger.handlers
    assert logger.background_listener is None
