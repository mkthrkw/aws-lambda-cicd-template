"""フローの1番目。処理対象を集めて次のステップへ渡す。"""

from common.logger import get_logger

STEP_NAME = "fetch"

logger = get_logger(__name__)


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """処理対象の一覧を返す。Step Functionsが戻り値を次のステップの入力にする。"""
    # 本来はS3やAPIから対象を取得します。サンプルでは固定値を返します。
    items = ["alpha", "beta", "gamma"]
    logger.info("%s: %d items", STEP_NAME, len(items))
    return {"step": STEP_NAME, "items": items}
