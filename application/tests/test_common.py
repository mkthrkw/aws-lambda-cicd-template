# application/common/ の共有コードに対するUnit Test。
# 共有コードはビルド時に各Lambdaのパッケージへ同梱されるため、
# 関数側のテストとは別に、ここで単体の動作を確認します。

import logging

from common.logger import get_logger
from common.utils import create_message


# 指定した名前のLoggerを返し、INFOレベルが有効になっていることを確認します。
def test_get_logger_returns_named_info_logger():
    logger = get_logger("sample.module")

    assert logger.name == "sample.module"
    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO)


# 同じ名前で呼び出した場合、Pythonのloggingが同一のLoggerを返すことを確認します。
def test_get_logger_returns_the_same_instance_for_one_name():
    assert get_logger("sample.module") is get_logger("sample.module")


# メッセージの形式を固定します。Lambdaの識別名がそのまま含まれます。
def test_create_message_includes_the_batch_name():
    assert create_message("sample_batch_a") == "Hello CI from sample_batch_a"
