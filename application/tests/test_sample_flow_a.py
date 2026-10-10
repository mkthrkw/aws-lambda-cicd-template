# sample-flow-aの3つのLambda handlerに対するUnit Test。
# AWSへは接続せず、Step Functionsが行う受け渡しをPythonの呼び出しで再現します。
#
# このRuntimeは「1 Runtime Stack = 1つのジョブ」の例です。
# fetch → transform → notify の順に、前のステップの戻り値が次の入力になります。

import logging

import pytest

from functions.sample_flow_a.fetch.handler import lambda_handler as fetch
from functions.sample_flow_a.notify.handler import lambda_handler as notify
from functions.sample_flow_a.transform.handler import lambda_handler as transform


# 3ステップをつないだときに、最初の件数が最後まで保たれることを確認します。
def test_steps_pass_results_to_the_next_step():
    fetched = fetch({}, None)
    assert fetched["items"] == ["alpha", "beta", "gamma"]

    transformed = transform(fetched, None)
    assert transformed["items"] == ["ALPHA", "BETA", "GAMMA"]

    result = notify(transformed, None)
    assert result["processed"] == 3
    assert result["step"] == "notify"


# 前のステップの結果が欠けている場合は失敗させます。
# 空の結果を返すと、何も処理していないのに成功したように見えるためです。
@pytest.mark.parametrize("handler", [transform, notify])
@pytest.mark.parametrize("event", [{}, None, {"items": "not-a-list"}, []])
def test_missing_previous_result_fails(handler, event):
    with pytest.raises(ValueError):
        handler(event, None)


# 各ステップが1行だけログへ記録することを確認します。
# CloudWatch Logsで、どのステップがどこまで進んだかを追えるようにするためです。
def test_each_step_logs_one_line(caplog):
    with caplog.at_level(logging.INFO):
        notify(transform(fetch({}, None), None), None)

    assert len(caplog.messages) == 3
