import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from configs.config import Config


def setup_logging(cfg: Config) -> None:
    """
    Configure root logging with both console and timed-rotating file handlers.
    """
    log_level = getattr(logging, (cfg.log_level or "INFO").upper(), logging.INFO)

    # Ensure log directory exists
    log_file_path = Path(cfg.log_file)
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    # Clear existing handlers (useful on reloads)
    for h in list(root.handlers):
        root.removeHandler(h)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # Timed rotating file handler (rotate at midnight, keep N days)
    fh = TimedRotatingFileHandler(
        filename=str(log_file_path),
        when="midnight",
        interval=1,
        backupCount=cfg.log_backup_count,
        utc=True,
        encoding="utf-8",
    )
    fh.setLevel(log_level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    logging.getLogger(__name__).info(
        "Logging initialized level=%s file=%s backups=%d",
        logging.getLevelName(log_level),
        str(log_file_path),
        cfg.log_backup_count,
    )

