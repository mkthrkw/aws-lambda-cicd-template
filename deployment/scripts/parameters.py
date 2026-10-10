"""Validate and resolve all Runtime parameters without AWS or SAM execution."""

# このファイルの役割
# ------------------
# CloudFormation / SAM のTemplateへ渡すParameter（設定値）を読み込み、検証し、
# 最終的な値を決めるところまでを担当します。AWSへの接続やSAMコマンドの実行は
# 行わないため、手元のPCでも安全に単体で動かせます。
#
# 値は次の4層を「あとの層が勝つ」順で重ねて決定します。
#   1. Templateの Parameters.<名前>.Default            （Templateの既定値）
#   2. infrastructure/runtimes/<runtime>/parameters.json（Runtime共通の上書き）
#   3. 環境ファイルの parameters                        （その環境の共通値）
#      このParameterを宣言しているRuntimeにだけ適用します。定期実行を持たない
#      Runtimeのように、宣言の異なるRuntimeが混在できるようにするためです。
#   4. 環境ファイルの runtimes.<runtime>                （その環境のRuntime個別）
#
# 仕様の詳細は infrastructure/env/README.md を参照してください。

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys

import yaml

# 入力として許可する名前の形。想定外の文字を早い段階で弾くために使います。
PARAMETER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*")            # 例: LogRetentionDays
RUNTIME_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")      # 例: sample-batch-a
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")  # 例: -1.5e3

# ProjectNameとRuntime名はStack名になり、Stack名はResource名の土台になります。
# どのResource名も上限の途中で切れない長さです（infrastructure/tests/test_resource_names.pyで確認）。
MAX_PROJECT_NAME_LENGTH = 20
MAX_RUNTIME_NAME_LENGTH = 20
# Stack名 <ProjectName>-<Environment>-<Runtime名> が、Bootstrap / CI Stackの名前と重ならないよう予約します。
RESERVED_RUNTIME_NAMES = frozenset({"bootstrap", "ci"})


def is_runtime_name(value) -> bool:
    # Runtime名の規則（形、長さ、予約名）をまとめて確認します。Build / Deploy / Parameter解決で共通です。
    return (
        isinstance(value, str)
        and RUNTIME_NAME.fullmatch(value) is not None
        and len(value) <= MAX_RUNTIME_NAME_LENGTH
        and value not in RESERVED_RUNTIME_NAMES
    )


class ParameterError(ValueError):
    """Invalid configuration; messages identify locations, not parameter values."""

    # 設定が不正なときに送出します。エラーメッセージには「どのファイルのどのキーか」
    # だけを載せ、値そのものは載せません（秘密値がログに出ることを防ぐため）。


class _TemplateLoader(yaml.SafeLoader):
    # SafeLoaderはPythonの任意オブジェクト生成を行わない安全な読込方式です。
    # ここではそれを継承し、CloudFormation独自のタグだけを扱えるようにします。
    pass


def _intrinsic(loader, tag, node):
    # Preserve tagged values as objects; do not evaluate CloudFormation functions.
    # `!Ref` や `!Sub` のようなCloudFormation組込み関数は、この処理では評価せず
    # `{"!Ref": "値"}` という辞書に置き換えて保持するだけにします。
    # 評価しないのは、実際の解決はCloudFormation側の責務だからです。
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)      # 例: !Ref Environment
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)    # 例: !Not [ ... ]
    else:
        value = loader.construct_mapping(node)     # 例: マッピング形式のタグ
    return {f"!{tag}": value}


# 「!」で始まるすべてのタグを上の処理へ回します（未知のタグでも読込に失敗しません）。
_TemplateLoader.add_multi_constructor("!", _intrinsic)


def _unique_object(pairs):
    # JSONは標準では同名キーが「あと勝ち」で黙って上書きされます。
    # 設定ミスを見逃さないよう、ここでは重複キーをエラーとして扱います。
    result = {}
    for key, value in pairs:
        if key in result:
            raise ParameterError("duplicate JSON key")
        result[key] = value
    return result


def load_json(path: Path) -> dict:
    # JSONファイルを読み込みます。読めない・壊れている・キーが重複している場合は
    # すべて ParameterError に変換し、呼び出し側で扱いやすくします。
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError) as exc:
        reason = "duplicate JSON key" if isinstance(exc, ParameterError) else "invalid or unreadable JSON"
        raise ParameterError(f"{path}: {reason}") from exc


def load_template(path: Path) -> dict:
    # SAM/CloudFormationのTemplate（YAML）を読み込みます。
    # Template全体が妥当かどうかの検証はSAM側（sam validate）に任せ、
    # ここでは「辞書として読めること」だけを確認します。
    try:
        template = yaml.load(path.read_text(encoding="utf-8"), Loader=_TemplateLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ParameterError(f"{path}: invalid or unreadable YAML") from exc
    if not isinstance(template, dict):
        raise ParameterError(f"{path}: template must be an object")
    return template


def _name(value, pattern, location):
    # Parameter名やRuntime名が、決めておいた命名規則に合っているかを確認します。
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ParameterError(f"{location}: invalid name")


def _string(value, location):
    # Parameterの値は必ず文字列です。数値は "14" のように文字列で書きます。
    if not isinstance(value, str):
        raise ParameterError(f"{location}: value must be a string")
    # バックスラッシュはSAM側のエスケープ解釈で値が変わる恐れがあるため、
    # 制御文字は取り扱いが環境差を生むため、初期仕様ではどちらも拒否します。
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ParameterError(f"{location}: backslashes and control characters are unsupported")
    return value


def _parameter_map(values, location, *, allow_environment=False):
    # 「Parameter名 → 値」の辞書（JSONの1ブロック）をまとめて検証します。
    if not isinstance(values, dict):
        raise ParameterError(f"{location}: parameter map must be an object")
    for key, value in values.items():
        _name(key, PARAMETER_NAME, location)
        # Environment（dev/stg/prod）は環境ファイルの共通値だけで決めます。
        # Runtime側から上書きできると、環境の取り違えに気付けなくなるためです。
        if key == "Environment" and not allow_environment:
            raise ParameterError(f"{location}: Environment is only allowed in environment.parameters")
        _string(value, f"{location}.{key}")


def _number(value, location):
    # Type: Number のParameterを検証します。文字列のまま比較すると "30" と "30.0" を
    # 別物と判定してしまうため、Decimalへ変換して数値として扱います。
    # （浮動小数点のfloatではなくDecimalを使うのは、桁落ちを避けるためです）
    if not NUMBER.fullmatch(value):
        raise ParameterError(f"{location}: expected a finite Number")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ParameterError(f"{location}: expected a finite Number") from exc
    if not result.is_finite():  # NaN や Infinity は受け付けません
        raise ParameterError(f"{location}: expected a finite Number")
    return result


def _template_value(value, parameter_type, location):
    # Template内のDefaultやAllowedValuesは、YAML上で 14 のような数値として
    # 書かれることがあります。JSON側の値（常に文字列）とそろえるため、
    # Number型に限り文字列へ正規化してから検証します。
    if parameter_type == "Number" and type(value) in (int, float):
        value = str(value)
    return _string(value, location)


def _resolve_runtime(template, layers, runtime):
    # 1つのRuntimeについて、Templateの宣言と上書き層（layers）から最終値を決めます。
    # layers は優先度の低い順に並んだ (値, そのRuntime向けか) の組のリストです。
    # そのRuntime向けの層は、Templateに無いParameterを書き間違いとして拒否します。
    # 環境共通の層は全Runtimeが対象のため、宣言の無いParameterは適用せず無視します。
    definitions = template.get("Parameters", {}) if isinstance(template, dict) else None
    if not isinstance(definitions, dict):
        raise ParameterError(f"{runtime}: Parameters must be an object")
    for layer, targets_this_runtime in layers:
        if targets_this_runtime and set(layer) - set(definitions):
            raise ParameterError(f"{runtime}: unknown parameter in configuration")
    resolved = {}
    for key, definition in definitions.items():
        _name(key, PARAMETER_NAME, runtime)
        location = f"{runtime}.{key}"
        # 対応する型は現在のTemplateで必要な String と Number だけです。
        # 未対応の型は黙って変換せず、必要になった時点で対応を追加します。
        if not isinstance(definition, dict) or definition.get("Type") not in ("String", "Number"):
            raise ParameterError(f"{location}: unsupported parameter Type")
        parameter_type = definition["Type"]
        # まずTemplateのDefaultを土台に置き、そのうえから層の順に上書きします。
        if "Default" in definition:
            resolved[key] = _template_value(definition["Default"], parameter_type, location)
        for layer, _ in layers:
            if key in layer:
                resolved[key] = layer[key]
        # Defaultも上書きも無い＝値が決まらないParameterは、デプロイ前に失敗させます。
        if key not in resolved:
            raise ParameterError(f"{location}: required parameter has no value")
        actual = resolved[key]
        if parameter_type == "Number":
            actual = _number(actual, location)
        # AllowedValues がある場合は、最終値がその中にあるかをここで確認します。
        if "AllowedValues" in definition:
            allowed = definition["AllowedValues"]
            if not isinstance(allowed, list) or not allowed:
                raise ParameterError(f"{location}: AllowedValues must be a nonempty array")
            allowed = [_template_value(value, parameter_type, location) for value in allowed]
            if parameter_type == "Number":
                allowed = [_number(value, location) for value in allowed]
            if actual not in allowed:
                raise ParameterError(f"{location}: value is not in AllowedValues")
    # 出力順を安定させるため、Parameter名の辞書順にそろえて返します。
    return dict(sorted(resolved.items()))


def resolve_all_parameters(templates: dict, runtime_parameters: dict, environment: dict,
                           deploy_env: str) -> dict[str, dict[str, str]]:
    """Return all resolved parameters, or raise before returning any partial result."""
    # 全Runtime分をまとめて解決します。途中で1つでも不正があれば例外となり、
    # 「一部だけ解決できた結果」を返すことはありません（部分デプロイの防止）。
    if deploy_env not in ("dev", "stg", "prod"):
        raise ParameterError("unsupported deployment environment")
    # Templateと設定ファイルの組が完全に一致していること（過不足なし）を確認します。
    if not templates or set(templates) != set(runtime_parameters):
        raise ParameterError("runtime template and parameter sets must match and be nonempty")
    for runtime in templates:
        if not is_runtime_name(runtime):
            raise ParameterError("runtime: invalid name")
    # 環境ファイルは parameters と runtimes の2キーだけを持つ約束です。
    # 想定外のキーはタイプミスの可能性があるため拒否します。
    if not isinstance(environment, dict) or set(environment) != {"parameters", "runtimes"}:
        raise ParameterError("environment must contain only parameters and runtimes")
    common = environment["parameters"]      # 全Runtime共通の環境値
    overrides = environment["runtimes"]     # 環境内のRuntime個別上書き
    _parameter_map(common, "environment.parameters", allow_environment=True)
    # 環境ファイルの Environment と、CIが渡した DEPLOY_ENV の食い違いを検出します。
    # 例: dev向けの設定でprod環境へデプロイしてしまう事故を防ぎます。
    if common.get("Environment") != deploy_env:
        raise ParameterError("environment.parameters.Environment does not match deployment environment")
    if not isinstance(overrides, dict):
        raise ParameterError("environment.runtimes must be an object")
    for runtime, values in overrides.items():
        if not is_runtime_name(runtime):
            raise ParameterError("environment.runtimes: invalid name")
        if runtime not in templates:
            raise ParameterError("environment.runtimes contains an unknown runtime")
        _parameter_map(values, f"environment.runtimes.{runtime}")
    # 環境共通値は、宣言しているRuntimeにだけ適用します。どのRuntimeも宣言して
    # いないParameterは、書き間違いか消し忘れのため、ここで失敗させます。
    declared = set()
    for template in templates.values():
        definitions = template.get("Parameters", {}) if isinstance(template, dict) else None
        if isinstance(definitions, dict):
            declared |= set(definitions)
    if set(common) - declared:
        raise ParameterError("environment.parameters contains a parameter no runtime declares")
    result = {}
    for runtime in sorted(templates):
        values = runtime_parameters[runtime]
        _parameter_map(values, runtime)
        # 第2層（Runtime共通）→ 第3層（環境共通）→ 第4層（環境×Runtime）の順に渡します。
        # 環境共通だけは、そのRuntimeが宣言していないParameterを無視します。
        result[runtime] = _resolve_runtime(
            templates[runtime],
            [(values, True), (common, False), (overrides.get(runtime, {}), True)],
            runtime,
        )
    return result


def resolve_parameter_files(template_paths: dict[str, Path], parameter_paths: dict[str, Path],
                            environment_path: Path, deploy_env: str) -> dict[str, dict[str, str]]:
    """Read explicit source or built-artifact paths; never fall back to other files."""
    # 上の関数のファイル版です。読む場所は呼び出し側が必ず明示します。
    # 指定のファイルが無いときに別の場所を探しにいく（フォールバック）ことはしません。
    # これにより、Source側とBuildArtifact側のどちらを読んだのかが常に明確になります。
    return resolve_all_parameters(
        {name: load_template(path) for name, path in template_paths.items()},
        {name: load_json(path) for name, path in parameter_paths.items()},
        load_json(environment_path), deploy_env,
    )


def sam_parameter_arguments(parameters: dict[str, str]) -> list[str]:
    """Produce argv elements for a future SAM caller, not a shell command."""
    # 解決済みの値を `sam deploy --parameter-overrides` へ渡す形に変換します。
    # 返すのは「コマンド文字列」ではなく「引数リストの要素」です。
    # subprocess へリストのまま渡すので、シェルによる再解釈（空白での分割など）は起きません。
    _parameter_map(parameters, "resolved parameters", allow_environment=True)
    arguments = []
    for key, value in sorted(parameters.items()):
        escaped = value.replace('"', '\\"')  # 値に含まれる二重引用符をSAM向けに退避
        arguments.append(f'ParameterKey={key},ParameterValue="{escaped}"')
    # Parameterが1つも無いときは、空のオプションを付けずに引数ごと省略します。
    return ["--parameter-overrides", *arguments] if arguments else []


def main(argv=None) -> int:
    # コマンドラインから設定だけを検証したいときの入口です。
    #   python -m deployment.scripts.parameters --environment dev
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("dev", "stg", "prod"))
    parser.add_argument("--runtimes-dir", type=Path, default=Path("infrastructure/runtimes"))
    parser.add_argument("--environment-file", type=Path)
    args = parser.parse_args(argv)
    # 環境ファイルは既定で infrastructure/env/<環境>/runtime-parameters.json です。
    environment_path = args.environment_file or Path(
        f"infrastructure/env/{args.environment}/runtime-parameters.json"
    )
    try:
        # ディレクトリ名がそのままRuntime名になります（一覧を別管理しません）。
        directories = list(args.runtimes_dir.iterdir())
        if any(not path.is_dir() for path in directories):
            raise ParameterError("runtimes directory must contain only runtime directories")
        resolved = resolve_parameter_files(
            {path.name: path / "template.yaml" for path in directories},
            {path.name: path / "parameters.json" for path in directories},
            environment_path, args.environment,
        )
        # Print only after every Runtime passed validation; never print values.
        # 値そのものは出力しません（秘密値の漏えい防止）。件数だけを表示します。
        for runtime, parameters in resolved.items():
            sam_parameter_arguments(parameters)
            print(f"{runtime}: parameters valid ({len(parameters)})")
    except (ParameterError, OSError) as exc:
        # 失敗時は終了コード1。CIはこれを見てStageを失敗させます。
        print(f"Parameter validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
