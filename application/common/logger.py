"""Provide the shared logger used by every function."""

import logging


def get_logger(name: str) -> logging.Logger:
    """Return an INFO level logger for the given module name."""
    # Lambdaの実行環境が用意する標準の出力先へそのまま書き出します。
    # 出力先の設定はTemplateのLoggingConfigで管理します。
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    return logger
