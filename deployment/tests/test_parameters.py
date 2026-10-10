# deployment/scripts/parameters.py のUnit Test。
# Parameterの読込・検証・4層の解決・SAM引数生成を、AWSへ接続せずに確認します。
#
# このファイルで使うpytestの仕組み
#   @pytest.fixture      … 各テストへ渡す準備データを作る関数。引数名で受け取ります。
#   @pytest.mark.parametrize … 値を変えて同じテストを繰り返します。
#   tmp_path             … テストごとに使い捨てのディレクトリ。実ファイルの読込検証に使います。
#   pytest.raises        … 指定した例外が起きることの確認。

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from deployment.scripts.parameters import (
    ParameterError,
    load_json,
    load_template,
    resolve_all_parameters,
    resolve_parameter_files,
    sam_parameter_arguments,
)

# リポジトリルート。このファイルから見て2つ上の階層です。
ROOT = Path(__file__).resolve().parents[2]  # リポジトリルート


@pytest.fixture
def inputs():
    # テスト用の入力3点セット。実ファイルではなくPythonの辞書で組み立てます。
    #   [0] Runtime名 → Template（Parameters宣言のみ）
    #   [1] Runtime名 → Runtime共通の上書き（第2層）
    #   [2] 環境Object（parameters=第3層、runtimes=第4層）
    # Daysは4層すべてに値があり、優先順位の確認に使います。
    return (
        {"sample-a": {"Parameters": {
            "Environment": {"Type": "String", "Default": "dev", "AllowedValues": ["dev", "stg", "prod"]},
            "Days": {"Type": "Number", "Default": 14, "AllowedValues": [14, 30, 60, 90]},
            "Topic": {"Type": "String", "Default": "default-topic"},
        }}},
        {"sample-a": {"Days": "30"}},
        {"parameters": {"Environment": "dev", "Days": "60"},
         "runtimes": {"sample-a": {"Days": "90"}}},
    )


def resolve(inputs):
    # 毎回同じ呼び出しを書かないための短縮関数です。
    return resolve_all_parameters(*inputs, deploy_env="dev")


# 優先順位の確認。上の層から順に削っていくと、下の層の値へ戻ることを確かめます。
# 90（環境×Runtime）→ 60（環境共通）→ 30（Runtime共通）→ 14（TemplateのDefault）。
# 併せて、解決処理が入力の辞書を書き換えていないことも確認します。
def test_priority_and_removal_fall_back_through_all_layers(inputs):
    original = copy.deepcopy(inputs)
    assert resolve(inputs)["sample-a"] == {"Days": "90", "Environment": "dev", "Topic": "default-topic"}
    assert inputs == original
    del inputs[2]["runtimes"]["sample-a"]["Days"]
    assert resolve(inputs)["sample-a"]["Days"] == "60"
    del inputs[2]["parameters"]["Days"]
    assert resolve(inputs)["sample-a"]["Days"] == "30"
    del inputs[1]["sample-a"]["Days"]
    assert resolve(inputs)["sample-a"]["Days"] == "14"


# 空文字は「未設定」ではなく有効な明示値です。下の層の値を空へ上書きできます。
def test_empty_string_replaces_lower_value(inputs):
    inputs[2]["runtimes"]["sample-a"]["Topic"] = ""
    assert resolve(inputs)["sample-a"]["Topic"] == ""


# 値の型と文字の検証。数値・真偽値・null・配列・Objectは使えません。
# バックスラッシュと制御文字（改行・タブ・NUL・DEL）も拒否します。
# 上の層で上書きされる層の値であっても、検証は省略しません。
@pytest.mark.parametrize("value", [14, True, None, [], {}, "bad\\path", "bad\nvalue", "bad\tvalue", "\x00", "\x7f"])
def test_rejects_invalid_parameter_values_even_in_overridden_layers(inputs, value):
    inputs[1]["sample-a"]["Days"] = value
    with pytest.raises(ParameterError):
        resolve(inputs)


# Templateに無いParameterを、そのRuntime向けの層へ書いた場合はエラーにします。
# 綴り間違いが黙って無視されると、設定したつもりの値が反映されないためです。
@pytest.mark.parametrize("location", ["runtime", "override"])
def test_unknown_parameter_in_a_runtime_layer_fails(inputs, location):
    layer = {"runtime": inputs[1]["sample-a"],
             "override": inputs[2]["runtimes"]["sample-a"]}[location]
    layer["Typo"] = "value"
    with pytest.raises(ParameterError, match="unknown parameter"):
        resolve(inputs)


# 環境共通値は全Runtimeが対象のため、どのRuntimeも宣言していない場合だけ失敗させます。
def test_unknown_parameter_in_the_common_layer_fails(inputs):
    inputs[2]["parameters"]["Typo"] = "value"
    with pytest.raises(ParameterError, match="no runtime declares"):
        resolve(inputs)


# Environmentは環境ファイルの共通値だけで決めます。Runtime側からは指定できません。
@pytest.mark.parametrize("location", ["runtime", "override"])
def test_environment_cannot_be_overridden_per_runtime(inputs, location):
    layer = inputs[1]["sample-a"] if location == "runtime" else inputs[2]["runtimes"]["sample-a"]
    layer["Environment"] = "dev"
    with pytest.raises(ParameterError, match="Environment is only allowed"):
        resolve(inputs)


# 環境Objectの構造検証。キー不足・余分なキー・型違いに加え、
# Environmentがdeploy_env（ここでは"dev"）と食い違う場合も拒否します。
# 環境の取り違えによる誤配備を防ぐための重要な確認です。
@pytest.mark.parametrize("environment", [None, [], {}, {"parameters": {}},
    {"parameters": {"Environment": "dev"}, "runtimes": {}, "extra": {}},
    {"parameters": [], "runtimes": {}},
    {"parameters": {}, "runtimes": {}},
    {"parameters": {"Environment": "prod"}, "runtimes": {}},
    {"parameters": {"Environment": "dev"}, "runtimes": []},
    {"parameters": {"Environment": "dev"}, "runtimes": {"unknown": {}}},
    {"parameters": {"Environment": "dev"}, "runtimes": {"sample-a": None}},
])
def test_rejects_invalid_environment_structure(inputs, environment):
    with pytest.raises(ParameterError):
        resolve_all_parameters(inputs[0], inputs[1], environment, "dev")


# Type: Number の値は有限の数値である必要があります。
# NaNやInfinity、桁あふれする指数表記、数値に見えない文字列を拒否します。
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "", "1_4", "text", "14 days", "1e9999999999999999999999"])
def test_rejects_invalid_number(inputs, value):
    inputs[2]["runtimes"]["sample-a"]["Days"] = value
    with pytest.raises(ParameterError, match="finite Number"):
        resolve(inputs)


# Numberは文字列としてではなく数値として比較します。
# "14.0"や"+14"や"1.4e1"は、AllowedValuesの14と同じ値とみなされます。
# ただしSAMへ渡す値は書いたままの文字列です（正規化しません）。
@pytest.mark.parametrize("value", ["14.0", "+14", "1.4e1"])
def test_number_allowed_values_compare_numerically(inputs, value):
    inputs[2]["runtimes"]["sample-a"]["Days"] = value
    assert resolve(inputs)["sample-a"]["Days"] == value


# AllowedValuesの範囲外を拒否します。後半は、Defaultをそのまま使う場合でも
# AllowedValuesの確認が働くことを確認しています。
def test_allowed_values_are_enforced(inputs):
    inputs[2]["runtimes"]["sample-a"]["Days"] = "15"
    with pytest.raises(ParameterError, match="AllowedValues"):
        resolve(inputs)
    inputs[2]["runtimes"]["sample-a"]["Days"] = "14"
    inputs[0]["sample-a"]["Parameters"]["Topic"]["AllowedValues"] = ["other"]
    with pytest.raises(ParameterError, match="AllowedValues"):
        resolve(inputs)


# Template側のParameter宣言が不正・未対応の場合を拒否します。
# 未対応の型（CommaDelimitedList）、値が決まらない宣言（Defaultも上書きも無い）、
# DefaultにObjectや無限大が入る場合、AllowedValuesの形が不正な場合が対象です。
@pytest.mark.parametrize("definition", [None, {}, {"Type": "CommaDelimitedList"},
    {"Type": "String"}, {"Type": "String", "Default": None},
    {"Type": "String", "Default": {"Ref": "Elsewhere"}},
    {"Type": "Number", "Default": True},
    {"Type": "Number", "Default": float("inf")},
    {"Type": "String", "Default": "x", "AllowedValues": "x"},
    {"Type": "String", "Default": "x", "AllowedValues": []},
])
def test_rejects_unsupported_or_incomplete_template_parameter(inputs, definition):
    inputs[0]["sample-a"]["Parameters"]["Topic"] = definition
    with pytest.raises(ParameterError):
        resolve(inputs)


# Runtime名の命名規則。アンダースコアや大文字、連続ハイフン、数字始まりは不可です。
# "../escape"のようなパス移動を含む名前も、ディレクトリ名として使うため拒否します。
# 21文字以上と、Bootstrap / CI Stackと同じStack名になる予約名（bootstrap、ci）も拒否します。
@pytest.mark.parametrize("name", ["Bad_Name", "../escape", "a--b", "1bad", "bad\n", "a" * 21, "bootstrap", "ci"])
def test_invalid_runtime_names(inputs, name):
    with pytest.raises(ParameterError, match="invalid name"):
        resolve_all_parameters({name: inputs[0]["sample-a"]}, {name: {}}, inputs[2], "dev")


# Templateと設定ファイルのRuntime集合は完全一致が必要です。
# 0件（設定ミス）、設定ファイルの欠落、名前違いのいずれも失敗させます。
def test_empty_and_mismatched_runtime_sets(inputs):
    for templates, values in [({}, {}), (inputs[0], {}), (inputs[0], {"sample-b": {}})]:
        with pytest.raises(ParameterError, match="sets must match"):
            resolve_all_parameters(templates, values, inputs[2], "dev")


# 環境共通値は、そのParameterを宣言しているRuntimeにだけ適用します。
# 定期実行を持つRuntimeと持たないRuntimeのように、宣言の異なるRuntimeが
# 同じ環境に並んでも、片方の設定がもう片方のデプロイを止めないようにするためです。
def test_common_parameter_applies_only_to_runtimes_that_declare_it(inputs):
    inputs[0]["sample-b"] = {"Parameters": {
        "Environment": {"Type": "String", "Default": "dev", "AllowedValues": ["dev", "stg", "prod"]},
    }}
    inputs[1]["sample-b"] = {}
    resolved = resolve(inputs)
    # Daysを宣言しないsample-bは、環境共通のDays（60）の影響を受けません。
    assert resolved["sample-b"] == {"Environment": "dev"}
    assert resolved["sample-a"]["Days"] == "90"


# 複数Runtimeの結果はRuntime名の辞書順に並び、互いに影響しません。
# alphaには個別上書きが無いため環境共通値の60、sample-aは個別上書きの90になります。
def test_multiple_runtime_results_are_sorted_and_independent(inputs):
    inputs[0]["alpha"] = copy.deepcopy(inputs[0]["sample-a"])
    inputs[1]["alpha"] = {}
    result = resolve(inputs)
    assert list(result) == ["alpha", "sample-a"]
    assert result["alpha"]["Days"] == "60"
    assert result["sample-a"]["Days"] == "90"


# JSONの重複キーは、どの階層にあってもエラーにします。
# 標準のJSON読込では後勝ちで黙って上書きされ、設定ミスに気付けないためです。
@pytest.mark.parametrize("text", ['{"A":"x","A":"y"}',
    '{"parameters":{"Environment":"dev","Environment":"prod"},"runtimes":{}}',
    '{"parameters":{},"runtimes":{"sample-a":{"Topic":"x","Topic":"y"}}}',
    '{"parameters":{},"runtimes":{"sample-a":{},"sample-a":{}}}',
])
def test_duplicate_json_keys_are_rejected_at_every_level(tmp_path, text):
    path = tmp_path / "parameters.json"
    path.write_text(text)
    with pytest.raises(ParameterError, match="duplicate JSON key"):
        load_json(path)


# ファイルが読めない場合の挙動をまとめて確認します。
#   1. 存在しないファイル
#   2. 壊れたJSON。このときエラーメッセージにファイルの中身を含めないことも確認します。
#   3. UTF-8として解釈できないバイト列
#   4. YAMLの危険なタグ。SafeLoaderを使うため、osコマンドは実行されずエラーになります。
def test_unreadable_or_invalid_files_fail(tmp_path):
    with pytest.raises(ParameterError):
        load_json(tmp_path / "missing.json")
    path = tmp_path / "invalid.json"
    path.write_text('{"secret": "do-not-echo", invalid}')
    with pytest.raises(ParameterError) as exc:
        load_json(path)
    assert "do-not-echo" not in str(exc.value)
    path.write_bytes(b"\xff")
    with pytest.raises(ParameterError):
        load_json(path)
    path.write_text("!!python/object/apply:os.system ['echo must-not-run']")
    with pytest.raises(ParameterError):
        load_template(path)


# CloudFormationの組込み関数は2通りの書き方ができます。
# 短縮形（!Sub）はSource側のTemplate、完全形（Fn::Sub）はsam buildの生成Templateに現れます。
# どちらを読んでもParameterを取り出せることを確認します。
def test_source_and_built_style_yaml_are_supported(tmp_path):
    path = tmp_path / "template.yaml"
    path.write_text('Parameters:\n  Name:\n    Type: String\n    Default: sample\nResources:\n  A: !Sub "${Name}"\n')
    assert load_template(path)["Parameters"]["Name"]["Default"] == "sample"
    path.write_text('Parameters:\n  Name:\n    Type: String\n    Default: sample\nResources:\n  A:\n    Fn::Sub: "${Name}"\n')
    assert load_template(path)["Parameters"]["Name"]["Default"] == "sample"


# SAMへ渡す引数の生成。空文字、空白、カンマ、日本語、引用符を含む値でも、
# 1つの値が1つの引数要素に収まることを確認します。
# "$(echo example)"が含まれるのは、シェルで評価されない形で渡すことの確認です。
@pytest.mark.parametrize("value", ["", "hello world", "a,b", "日本語", 'say "hello"', '"quoted"', "it's fine", "$(echo example)"])
def test_sam_argv_preserves_one_argument_per_value(value):
    escaped = value.replace('"', '\\"')
    assert sam_parameter_arguments({"Value": value}) == [
        "--parameter-overrides", f'ParameterKey=Value,ParameterValue="{escaped}"'
    ]


# 引数はParameter名の辞書順に並びます。生成時にも入力検証を行い、
# Parameterが1件も無い場合は--parameter-overrides自体を付けません。
def test_sam_arguments_are_sorted_and_validate_inputs():
    assert sam_parameter_arguments({"Z": "", "A": "value"}) == [
        "--parameter-overrides", 'ParameterKey=A,ParameterValue="value"',
        'ParameterKey=Z,ParameterValue=""',
    ]
    for values in [{"bad-key": "x"}, {"Value": "bad\\path"}, {"Value": 14}]:
        with pytest.raises(ParameterError):
            sam_parameter_arguments(values)
    assert sam_parameter_arguments({}) == []


# ここまでは作り物の入力でしたが、このテストはリポジトリの実ファイルを使います。
# 3環境すべてで解決でき、CLIとして実行しても成功することを確認します。
# subprocessで実際にコマンドを起動し、標準出力に値が出ていないことも確かめます。
@pytest.mark.parametrize("environment", ["dev", "stg", "prod"])
def test_repository_settings_and_cli(environment):
    runtime = ROOT / "infrastructure/runtimes/sample-batch-a"
    resolved = resolve_parameter_files(
        {"sample-batch-a": runtime / "template.yaml"},
        {"sample-batch-a": runtime / "parameters.json"},
        ROOT / f"infrastructure/env/{environment}/runtime-parameters.json", environment,
    )
    assert resolved == {"sample-batch-a": {
        "AlarmTopicArn": "", "Environment": environment, "LogRetentionDays": "14", "ProjectName": "",
        "ScheduleExpression": "cron(0 3 * * ? *)", "ScheduleState": "DISABLED",
        "ScheduleTimezone": "Asia/Tokyo",
    }}
    result = subprocess.run([sys.executable, "-m", "deployment.scripts.parameters", "--environment", environment],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "sample-batch-a: parameters valid (7)\n"
        "sample-flow-a: parameters valid (7)\n"
        # 定期実行を持たないRuntimeは、環境共通のScheduleStateの影響を受けません。
        "sample-queue-a: parameters valid (6)\n"
    )


# 2件目のRuntime（beta）で失敗する状況を作り、次の2点を確認します。
#   - 1件目（alpha）が成功していても「一部成功」の出力を残さない（標準出力が空）
#   - エラーには場所（beta）だけを出し、設定値そのものを出さない
def test_cli_late_runtime_failure_has_no_partial_success_or_value_output(tmp_path):
    root = tmp_path / "runtimes"
    for name in ["alpha", "beta"]:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "template.yaml").write_text("Parameters:\n  Environment:\n    Type: String\n")
        (directory / "parameters.json").write_text("{}")
    (root / "beta/parameters.json").write_text('{"Unknown":"do-not-echo"}')
    environment = tmp_path / "env.json"
    environment.write_text(json.dumps({"parameters": {"Environment": "dev"}, "runtimes": {}}))
    result = subprocess.run([
        sys.executable, "-m", "deployment.scripts.parameters", "--environment", "dev",
        "--runtimes-dir", str(root), "--environment-file", str(environment),
    ], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "beta" in result.stderr
    assert "do-not-echo" not in result.stderr
