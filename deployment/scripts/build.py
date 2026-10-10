"""Build every Runtime and assemble a relocatable BuildArtifact; never deploy."""

# このファイルの役割
# ------------------
# CI/CDのBuild Stageで動き、全Runtime（Lambda 1つ分のまとまり）をビルドして
# `build/` 配下に「そのまま別環境へ持ち運べる成果物（BuildArtifact）」を作ります。
# AWSへの配備は一切行いません。配備は deploy.py の担当です。
#
# 処理の流れ
#   1. 入力を全件検証（Parameter、Template、CodeUriの位置など）
#   2. 全Runtimeに sam validate --lint
#   3. 全Runtimeに sam build
#   4. 共有コード（application/common）を各成果物へ同梱
#   5. 設定ファイル（Parameter JSON）を成果物へコピー
#   6. 出来上がった成果物をソースコード無しで再検証
# 途中で失敗した場合は `build/` を丸ごと削除し、中途半端な成果物を残しません。
#
# 生成物の構造（詳細は deployment/README.md の「BuildArtifactの構造」）
#   build/
#   ├ runtimes/<runtime>/template.yaml と ビルド済みコード
#   └ configuration/runtime-parameters.json と runtimes/<runtime>.json

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

from deployment.scripts.parameters import (
    MAX_RUNTIME_NAME_LENGTH,
    ParameterError,
    RESERVED_RUNTIME_NAMES,
    is_runtime_name,
    load_template,
    resolve_parameter_files,
)


class BuildError(ValueError):
    """Invalid build input or incomplete artifact."""

    # 入力が不正、または生成した成果物が不完全なときに送出します。


def _runtime_directories(path: Path) -> dict[str, Path]:
    # Runtimeの一覧は「ディレクトリがそこにあること」がすべてです。
    # 別途Runtime一覧ファイルを持たないため、ここで中身の形だけを厳しく確認します。
    entries = list(path.iterdir())
    if not entries:
        # 対象0件は「設定ミス」とみなして失敗させます（黙って何もしないを避けるため）。
        raise BuildError(f"{path}: no runtimes found")
    # ディレクトリであること、シンボリックリンクでないことを確認します。
    if any(not entry.is_dir() or entry.is_symlink() for entry in entries):
        raise BuildError(f"{path}: expected named runtime directories")
    # Runtime名はStack名とResource名の一部になるため、形・長さ・予約名を確認します。
    invalid = sorted(entry.name for entry in entries if not is_runtime_name(entry.name))
    if invalid:
        raise BuildError(
            f"{path}: invalid runtime names: {', '.join(invalid)} "
            f"(lowercase letters, numbers, and hyphens; at most {MAX_RUNTIME_NAME_LENGTH} characters; "
            f"not {' or '.join(sorted(RESERVED_RUNTIME_NAMES))})"
        )
    # Runtime名の辞書順で安定した処理順になるよう sorted しています。
    return {entry.name: entry for entry in sorted(entries)}


def _functions(template: dict, location: Path) -> dict[str, dict]:
    # Templateからビルド対象のLambda関数を取り出します。
    # 1 Runtime Stackは1つのデプロイ単位で、単独の関数にも、複数の関数を持つ
    # ジョブ（Step Functionsのフローなど）にもなります。想定外の構成は明示的に拒否します。
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        raise BuildError(f"{location}: Resources must be an object")
    functions = {}
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            raise BuildError(f"{location}: invalid Resource")
        resource_type = resource.get("Type", "")
        if resource_type == "AWS::Serverless::Function":
            functions[logical_id] = resource.get("Properties")
        elif resource_type in ("AWS::Serverless::LayerVersion", "AWS::Serverless::Application",
                                "AWS::Lambda::Function"):
            # Layerや入れ子Applicationはビルド手順が変わるため、現時点では未対応です。
            raise BuildError(f"{location}: only SAM functions can be built in a runtime")
    if not functions or any(not isinstance(item, dict) for item in functions.values()):
        raise BuildError(f"{location}: expected at least one SAM function")
    for logical_id, properties in functions.items():
        # 対象はZIP形式（ローカルのコードを固めて配布する方式）のみです。
        # コンテナImage形式はビルドと配備の流れが異なるため別途設計します。
        if properties.get("PackageType", "Zip") != "Zip":
            raise BuildError(f"{location}: {logical_id}: only local Zip applications are supported")
        # RuntimeとArchitecturesは環境で変わらない固定値であることを求めます。
        # 環境ごとにビルド結果が変わると、同じArtifactを検証した意味が薄れるためです。
        if not isinstance(properties.get("Runtime"), str):
            raise BuildError(f"{location}: {logical_id}: Runtime must be a fixed string")
        if properties.get("Architectures") not in (["x86_64"], ["arm64"]):
            raise BuildError(f"{location}: {logical_id}: Architectures must be one fixed architecture")
    # 論理IDの辞書順で安定した処理順にします。
    return dict(sorted(functions.items()))


def _code_directories(template_path: Path, boundary: Path) -> dict[str, Path]:
    # 各関数のCodeUri（ソースコードの場所）を解決します。
    # boundary は「ここから外を参照してはいけない」という境界ディレクトリです。
    result = {}
    for logical_id, properties in _functions(load_template(template_path), template_path).items():
        uri = properties.get("CodeUri")
        # S3のURIや絶対パスを許すとArtifactを持ち運べなくなるため、相対パスだけを許可します。
        if not isinstance(uri, str) or not uri or "://" in uri or Path(uri).is_absolute():
            raise BuildError(f"{template_path}: {logical_id}: CodeUri must be a relative local directory")
        code = (template_path.parent / uri).resolve()
        # 境界の外（例: リポジトリ外）を指していないか、実在するかを確認します。
        if not code.is_relative_to(boundary.resolve()) or not code.is_dir():
            raise BuildError(f"{template_path}: {logical_id}: CodeUri is missing or outside its boundary")
        result[logical_id] = code
    return result


def validate_artifact(artifact: Path, expected_runtimes: set[str], environment: str) -> dict[str, dict[str, str]]:
    """Validate generated paths and parameters without the application source tree."""
    # 出来上がったBuildArtifactだけを見て検証します。元のソースツリーは参照しません。
    # そのためBuild直後だけでなく、Deploy側（別のCodeBuild作業ディレクトリ）でも
    # まったく同じ関数で検証できます。戻り値は全Runtimeの解決済みParameterです。
    directories = _runtime_directories(artifact / "runtimes")
    # 期待したRuntime名の集合と完全一致すること（欠落も余剰も不可）を確認します。
    if set(directories) != expected_runtimes:
        raise BuildError("artifact runtime set does not match the source")
    parameter_directory = artifact / "configuration/runtimes"
    parameter_entries = list(parameter_directory.iterdir())
    if any(not path.is_file() or path.is_symlink() or path.suffix != ".json"
           for path in parameter_entries):
        raise BuildError("artifact runtime parameter files must be JSON files")
    # 設定ファイル名（拡張子を除いた部分）もRuntime名と一致している必要があります。
    if {path.stem for path in parameter_entries} != expected_runtimes:
        raise BuildError("artifact runtime parameter set does not match the source")
    # Symlinks do not have a portable representation with the default Artifact settings.
    # CodePipelineのArtifactはシンボリックリンクの扱いが環境で異なるため拒否します。
    if any(path.is_symlink() for path in artifact.rglob("*")):
        raise BuildError("artifact must not contain symlinks")
    # 生成Template側のCodeUriが、そのRuntimeディレクトリ内で解決できることを確認します。
    for directory in directories.values():
        _code_directories(directory / "template.yaml", directory)
    # 最後にParameterを全件解決します（1つでも不正ならここで例外になります）。
    return resolve_parameter_files(
        {name: directory / "template.yaml" for name, directory in directories.items()},
        {path.stem: path for path in parameter_entries},
        artifact / "configuration/runtime-parameters.json", environment,
    )


def _clear_output(output: Path) -> None:
    # 出力先 `build/` を空にします。前回の成果物が混ざるのを防ぐためです。
    # 想定外の対象（シンボリックリンクやファイル）を消してしまわないよう先に確認します。
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise BuildError(f"{output}: refusing to clear a symlink or non-directory")
    if output.exists():
        shutil.rmtree(output)


def build_artifact(source_root: Path, environment: str, region: str) -> Path:
    """Replace source_root/build with a complete artifact, removing it on failure."""
    source_root = source_root.resolve()
    output = source_root / "build"
    _clear_output(output)
    complete = False  # 最後まで到達したかどうかの目印（finallyでの後片付け判定に使用）
    try:
        if environment not in ("dev", "stg", "prod") or not region:
            raise BuildError("environment and region are required")
        directories = _runtime_directories(source_root / "infrastructure/runtimes")
        templates = {name: directory / "template.yaml" for name, directory in directories.items()}
        parameters = {name: directory / "parameters.json" for name, directory in directories.items()}
        environment_file = source_root / f"infrastructure/env/{environment}/runtime-parameters.json"
        # まずParameterを全件解決し、設定ミスがあればビルド前に止めます。
        resolve_parameter_files(templates, parameters, environment_file, environment)
        # Check all inputs before the first build, including fixed build properties.
        # 時間のかかるビルドを始める前に、全Runtimeの入力をまとめて確認します。
        shared_code = source_root / "application/common"
        if not shared_code.is_dir():
            raise BuildError(f"{shared_code}: shared code directory is required")
        if any(path.is_symlink() for path in shared_code.rglob("*")):
            raise BuildError(f"{shared_code}: shared code symlinks are unsupported")
        for template in templates.values():
            for code in _code_directories(template, source_root).values():
                # コードの置き場と出力先が重なると、ビルド中に自分の入力を壊してしまいます。
                if output.is_relative_to(code) or code.is_relative_to(output):
                    raise BuildError(f"{template}: CodeUri overlaps the build output")
                if any(path.is_symlink() for path in code.rglob("*")):
                    raise BuildError(f"{template}: source code symlinks are unsupported")
        # 第1段階: Templateの静的検査（cfn-lint併用）。AWSへの変更は起きません。
        for name, template in templates.items():
            print(f"{name}: validating template", flush=True)
            subprocess.run([
                "sam", "validate", "--lint", "--region", region,
                "--template-file", str(template),
            ], cwd=source_root, check=True)
        # 第2段階: 依存ライブラリを含めたビルド。Runtimeごとに別ディレクトリへ出力します。
        for name, template in templates.items():
            print(f"{name}: building", flush=True)
            subprocess.run([
                "sam", "build", "--template-file", str(template),
                "--build-dir", str(output / "runtimes" / name),
            ], cwd=source_root, check=True)
        # SAM writes its build graph beside the specified build directory.
        # It is local build state, not part of the deployable Runtime set.
        # build.toml はSAMのローカル作業ファイルです。Runtime一覧に混ざると
        # 「Runtime名の集合が一致しない」検証で誤検知するため取り除きます。
        (output / "runtimes/build.toml").unlink(missing_ok=True)
        # 第3段階: 共有コードを各Lambdaのパッケージへ同梱します。
        # SAMはCodeUriの外にあるファイルを扱わないため、ビルド後にここでコピーします。
        # 1つのRuntimeに複数の関数がある場合は、そのすべてへ同梱します。
        for name in directories:
            runtime_output = output / "runtimes" / name
            for code in _code_directories(runtime_output / "template.yaml", runtime_output).values():
                shutil.copytree(shared_code, code / "common",
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 第4段階: 設定ファイルを成果物へコピーします（内容は変更しません）。
        # 環境ファイルは対象の1環境分だけを同梱し、別環境への流用を防ぎます。
        configuration = output / "configuration"
        (configuration / "runtimes").mkdir(parents=True)
        for name, path in parameters.items():
            shutil.copyfile(path, configuration / "runtimes" / f"{name}.json")
        shutil.copyfile(environment_file, configuration / "runtime-parameters.json")
        # 第5段階: Deploy側と同じ関数で、成果物だけを見た検証を行います。
        validate_artifact(output, set(directories), environment)
        complete = True
        print(f"BuildArtifact ready: {output}", flush=True)
        return output
    finally:
        # 失敗時は不完全な `build/` を残しません。次のStageへ渡らないようにするためです。
        if not complete:
            _clear_output(output)


def main(argv=None) -> int:
    # コマンドラインからの入口です。
    #   python -m deployment.scripts.build --environment dev --region ap-northeast-1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("dev", "stg", "prod"))
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    try:
        build_artifact(Path.cwd(), args.environment, args.region)
    except subprocess.CalledProcessError as exc:
        # SAMコマンド自体が失敗した場合。終了コードをできる限りそのまま伝えます。
        print(f"SAM command failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode if 0 < exc.returncode < 256 else 1
    except (BuildError, ParameterError, OSError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
