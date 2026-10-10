# pytestの実行前に、各Lambdaのディレクトリをimport元へ追加します。
#
# Lambdaの実行環境では、その関数のディレクトリ（TemplateのCodeUriが指す場所）が
# パッケージのルートになります。handler.pyと同じ階層に置いたモジュールは
# `import transform`のように参照できます。
# テストからは`functions.<関数名>.handler`としてimportするため、この解決が働きません。
# ここでsys.pathへ追加し、実行環境と同じimportが通るようにします。
#
# 関数のディレクトリは1階層とは限りません。複数の関数で1つのジョブを作る場合は
# `functions/<ジョブ名>/<ステップ名>/`のようにまとめます。そのため、置き場所ではなく
# 「handler.pyがあること」を目印に探します。
#
# 関数をまたいで同じ名前のモジュールを置くと、どちらが解決されるか決まりません。
# 補助モジュールの名前は関数間で重複させないか、共有コードへ置いてください。

import sys
from pathlib import Path

FUNCTIONS = Path(__file__).resolve().parents[1] / "functions"

for handler in sorted(FUNCTIONS.rglob("handler.py")):
    if "__pycache__" not in handler.parts:
        sys.path.insert(0, str(handler.parent))
