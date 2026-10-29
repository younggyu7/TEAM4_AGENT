import logging

LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"


class _RequestIdDefault(logging.Filter):
    """request_id가 없는 레코드(시작 로그, 라이브러리 로그)도 포맷이 깨지지 않게 '-'를 채웁니다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


def setup_logging(level: str = "INFO") -> None:
    # SDK 디버그 로그는 요청/응답 본문을 포함할 수 있으므로 앱 로그 레벨과 분리한다.
    for name in ("anthropic", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdDefault())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def get_request_logger(request_id: str) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger("app"), {"request_id": request_id})
