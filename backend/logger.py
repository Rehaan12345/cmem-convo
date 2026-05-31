import logging
import sys


def get_logger(name: str, log_file: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        if log_file:
            fh = logging.FileHandler(log_file)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        logger.setLevel(logging.INFO)
    return logger
