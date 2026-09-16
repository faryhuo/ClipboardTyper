"""Background log writing and rotation."""
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
import copy
import logging
import queue


class DeferredLogHandler(QueueHandler):
    def prepare(self, record):
        # Keep traceback formatting and file rotation on the consumer thread too.
        return copy.copy(record)


def close_logger(logger):
    listener = getattr(logger, "background_listener", None)
    if listener:
        listener.stop()  # Drain after the keyboard hook has been removed.
        for handler in listener.handlers:
            handler.close()
        logger.background_listener = None
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def make_logger(path):
    logger = logging.getLogger("ClipboardTyper")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    close_logger(logger)
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    messages = queue.SimpleQueue()
    logger.addHandler(DeferredLogHandler(messages))
    logger.background_listener = QueueListener(messages, handler)
    logger.background_listener.start()
    return logger
