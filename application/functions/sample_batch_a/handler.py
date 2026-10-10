"""Report one scheduled run without calling external services."""

from common.logger import get_logger
from common.utils import create_message

BATCH_NAME = "sample_batch_a"

logger = get_logger(__name__)


def lambda_handler(event: object, context: object) -> dict[str, str]:
    """Log one execution and return the batch name with its message."""
    # EventBridge Schedulerは固定の入力を渡します。この関数は入力を参照しません。
    # 案件固有の処理へ置き換えるときは、ここから業務処理を呼び出します。
    message = create_message(BATCH_NAME)
    logger.info(message)
    return {"batch": BATCH_NAME, "message": message}
