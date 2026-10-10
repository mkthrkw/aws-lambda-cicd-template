"""フローの3番目。結果をログへ残して締めくくる。"""

from common.logger import get_logger
from common.utils import create_message

STEP_NAME = "notify"

logger = get_logger(__name__)


def lambda_handler(event: dict, context: object) -> dict[str, object]:
    """加工結果の件数をログへ記録し、ジョブの結果を返す。"""
    items = event.get("items") if isinstance(event, dict) else None
    if not isinstance(items, list):
        raise ValueError("items must be provided by the previous step")
    # 実際の案件では、ここから通知や書き込みを行います。
    message = create_message(f"sample_flow_a ({len(items)} items)")
    logger.info(message)
    return {"step": STEP_NAME, "processed": len(items), "message": message}
