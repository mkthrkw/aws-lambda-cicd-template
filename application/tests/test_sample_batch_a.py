# sample-batch-aのLambda handlerに対するUnit Test。
# AWSへは接続せず、Pythonの関数を直接呼び出して入出力を確認します。
#
# この関数はEventBridge Schedulerから定期実行されるサンプルです。
# CI/CDと実行基盤が正しく動くことを確認するための最小限の処理だけを持ち、
# 案件固有の業務処理へ置き換えて使うことを想定しています。
#
# pytestの基本
#   - 関数名が test_ で始まるものが自動的にテストとして実行されます。
#   - assert が真なら成功、偽なら失敗です。
#   - caplog はログ出力を捕まえるpytestの機能です。

import logging

import pytest
from functions.sample_batch_a.handler import lambda_handler


# 戻り値の形を固定します。Lambdaの識別名と、共有コードが作るメッセージを返します。
def test_returns_batch_name_and_message():
    assert lambda_handler({}, None) == {
        "batch": "sample_batch_a",
        "message": "Hello CI from sample_batch_a",
    }


# Schedulerは固定の入力を渡します。入力の内容によって結果が変わらないことを確認します。
@pytest.mark.parametrize("event", [{}, None, {"unexpected": "value"}, []])
def test_ignores_event_content(event):
    assert lambda_handler(event, None)["batch"] == "sample_batch_a"


# 1回の実行につき1行だけログへ記録します。
# 実行されたことをCloudWatch Logsから確認できるようにするためのログです。
def test_logs_one_line_per_execution(caplog):
    with caplog.at_level(logging.INFO):
        lambda_handler({}, None)

    assert caplog.messages == ["Hello CI from sample_batch_a"]
