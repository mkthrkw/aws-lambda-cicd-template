# sample-queue-aのhandlerに対するUnit Test。
# AWSへは接続せず、Event Source Mappingが渡すイベントの形をPythonの辞書で再現します。
#
# このRuntimeは「メッセージが届いたら動く」例です。1件の失敗で成功分まで
# 再処理されないよう、失敗したメッセージだけを戻す形を確認します。

import logging

import pytest

from functions.sample_queue_a.handler import lambda_handler


def event(*bodies):
    # Event Source Mappingが渡すイベントのうち、handlerが使う項目だけを組み立てます。
    return {"Records": [
        {"messageId": f"id-{index}", "body": body} for index, body in enumerate(bodies)
    ]}


# すべて成功した場合は、戻す失敗が1件も無いことを確認します。
# 空のリストを返すことで、Lambdaは全件を処理済みとしてキューから削除します。
def test_all_messages_succeed():
    assert lambda_handler(event("alpha", "beta"), None) == {"batchItemFailures": []}


# 失敗したメッセージだけがキューへ戻ることを確認します。
# 成功した分まで戻すと、同じ処理を二重に行ってしまいます。
def test_only_the_failed_message_is_reported():
    result = lambda_handler(event("alpha", "fail", "gamma"), None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "id-1"}]}


# 大文字・空白を含む書き方でも、失敗させる入口として同じように扱います。
@pytest.mark.parametrize("body", ["fail", "FAIL", " Fail "])
def test_failure_keyword_is_case_insensitive(body):
    result = lambda_handler(event(body), None)

    assert result["batchItemFailures"] == [{"itemIdentifier": "id-0"}]


# Event Source Mapping以外から呼ばれた場合は失敗させます。
# 何も処理していないのに成功したように見える状態を避けるためです。
@pytest.mark.parametrize("bad_event", [{}, None, {"Records": "not-a-list"}])
def test_event_without_records_fails(bad_event):
    with pytest.raises(ValueError):
        lambda_handler(bad_event, None)


# 処理の結果が1行にまとまってログへ残ることを確認します。
# 何件届いて何件失敗したかを、CloudWatch Logsから追えるようにするためです。
def test_logs_the_number_of_received_and_failed_messages(caplog):
    with caplog.at_level(logging.INFO):
        lambda_handler(event("alpha", "fail"), None)

    assert "2 received, 1 failed" in caplog.records[-1].getMessage()
