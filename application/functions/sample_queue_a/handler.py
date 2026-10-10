"""キューに届いたメッセージを1件ずつ処理し、失敗したものだけを報告する。"""

from common.logger import get_logger

QUEUE_NAME = "sample_queue_a"

# 本文がこの値のメッセージだけ、わざと失敗させます。再試行とDLQの動きを
# 実環境で確認するためのサンプル用の入口です。業務処理へ置き換えるときは削除します。
FAILURE_KEYWORD = "fail"

logger = get_logger(__name__)


def lambda_handler(event: dict, context: object) -> dict[str, list[dict[str, str]]]:
    """1回の呼び出しで届いたメッセージを処理し、失敗した分を返す。

    戻り値の batchItemFailures に入れたメッセージだけがキューへ戻ります。
    成功した分まで戻さないため、同じ処理を二重に行わずに済みます。
    この仕組みは、Templateの FunctionResponseTypes: ReportBatchItemFailures と
    対になっています。片方だけでは働きません。
    """
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        # Lambdaは常に Records を渡します。無い場合は呼び出し方の誤りです。
        raise ValueError("Records must be provided by the event source mapping")

    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId", "")
        try:
            process_message(record.get("body", ""))
        except Exception:
            # 1件の失敗で残りを巻き込まないよう、ここで受け止めて記録します。
            logger.exception("%s: failed messageId=%s", QUEUE_NAME, message_id)
            failures.append({"itemIdentifier": message_id})

    logger.info("%s: %d received, %d failed", QUEUE_NAME, len(records), len(failures))
    return {"batchItemFailures": failures}


def process_message(body: str) -> None:
    """1件のメッセージを処理する。ここを業務処理へ置き換えます。"""
    if body.strip().lower() == FAILURE_KEYWORD:
        raise ValueError(f"message body is {FAILURE_KEYWORD!r}")
    logger.info("%s: processed %r", QUEUE_NAME, body)
