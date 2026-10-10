"""フローの2番目。1番目が集めた対象を加工する。"""

from common.logger import get_logger

STEP_NAME = "transform"

logger = get_logger(__name__)


def lambda_handler(event: dict, context: object) -> dict[str, object]:
    """入力の items を加工して返す。

    入力はStep Functionsが渡す前のステップの戻り値です。想定した形でない場合は
    例外を送出し、ジョブを失敗させます。黙って空の結果を返すと、後続が
    成功したように見えてしまうためです。
    """
    items = event.get("items") if isinstance(event, dict) else None
    if not isinstance(items, list):
        raise ValueError("items must be provided by the previous step")
    transformed = [str(item).upper() for item in items]
    logger.info("%s: %d items", STEP_NAME, len(transformed))
    return {"step": STEP_NAME, "items": transformed}
