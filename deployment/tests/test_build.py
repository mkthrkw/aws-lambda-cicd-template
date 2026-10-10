# deployment/scripts/build.py のUnit Test。
#
# 重要な前提: SAM CLIは実際には呼び出しません。
# monkeypatchでsubprocess.runを差し替え、sam validate / sam buildが「呼ばれたこと」と
# 「呼ばれなかったこと」を記録して確認します。これにより、SAMやAWS認証が無い環境でも
# ビルド処理の判断（検証順序・失敗時の後片付け）を高速に検証できます。
#
# 作業用のファイルはtmp_path（テストごとの使い捨てディレクトリ）に作り、
# リポジトリ本体を書き換えません。

import copy
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from deployment.scripts import build
from deployment.scripts.parameters import ParameterError, load_template

ROOT = Path(__file__).resolve().parents[2]  # リポジトリルート


@pytest.fixture
def source(tmp_path):
    # 本物のリポジトリを模したソースツリーを組み立てます。
    # ディレクトリ名に空白を入れているのは、パスの引用漏れがあれば失敗させるためです。
    root = tmp_path / "source with spaces"
    code = root / "application/functions/sample_batch_a"
    code.mkdir(parents=True)
    shutil.copyfile(ROOT / "application/functions/sample_batch_a/handler.py", code / "handler.py")
    # 共有コードはCodeUriの外にあり、ビルド後にbuild.pyが成果物へ同梱します。
    shutil.copytree(ROOT / "application/common", root / "application/common",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # 辞書順とは逆の順（zeta → alpha）で作り、処理側が名前順に並べ替えることを確認できるようにします。
    for name in ["zeta", "alpha"]:
        runtime = root / "infrastructure/runtimes" / name
        runtime.mkdir(parents=True)
        shutil.copyfile(ROOT / "infrastructure/runtimes/sample-batch-a/template.yaml", runtime / "template.yaml")
        (runtime / "parameters.json").write_text('{}\n')
    environment = root / "infrastructure/env/dev"
    environment.mkdir(parents=True)
    shutil.copyfile(ROOT / "infrastructure/env/dev/runtime-parameters.json", environment / "runtime-parameters.json")
    return root


@pytest.fixture
def sam(monkeypatch):
    # SAM CLIの代役。呼び出し内容をcallsへ記録し、sam buildのときは
    # 本物と同じ形の生成物（ビルド済みコード、CodeUriを書き換えたTemplate、build.toml）を作ります。
    calls = []

    def run(argv, *, cwd, check):
        # シェルを介さずリスト形式で、かつ失敗を検出する設定で呼ばれていることを確認します。
        assert check is True
        assert isinstance(argv, list)
        template = Path(argv[argv.index("--template-file") + 1])
        calls.append((argv[1], template.parent.name))   # 例: ("build", "alpha")
        if argv[1] == "build":
            destination = Path(argv[argv.index("--build-dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            # SAMは指定した出力先の隣にbuild.tomlを書きます。これを再現します。
            (destination.parent / "build.toml").write_text("# SAM build metadata\n")
            data = load_template(template)
            # SAMは関数ごとに「論理ID」という名前のディレクトリへビルド結果を置きます。
            # 生成TemplateのCodeUriも、その相対パスへ書き換わります。
            for logical_id, resource in data["Resources"].items():
                if resource.get("Type") != "AWS::Serverless::Function":
                    continue
                code = destination / logical_id
                code.mkdir(parents=True)
                shutil.copyfile(Path(cwd) / "application/functions/sample_batch_a/handler.py",
                                code / "handler.py")
                resource["Properties"]["CodeUri"] = logical_id
            (destination / "template.yaml").write_text(yaml.safe_dump(data))
        return subprocess.CompletedProcess(argv, 0)

    # build.py内のsubprocess.runを、このテスト中だけ上のrunへ差し替えます。
    monkeypatch.setattr(build.subprocess, "run", run)
    return calls, run


# 正常系。次の4点を確認します。
#   - 全件validate → 全件build の順で、Runtime名の辞書順に処理する
#   - 前回の成果物（removed）が残らない
#   - SAMの作業ファイルbuild.tomlがArtifactに混ざらない
#   - 設定ファイルが1バイトも変えずにコピーされる
def test_builds_every_runtime_in_order_and_preserves_configuration(source, sam):
    old = source / "build/runtimes/removed"
    old.mkdir(parents=True)
    (old / "stale.txt").write_text("old")
    artifact = build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == [("validate", "alpha"), ("validate", "zeta"), ("build", "alpha"), ("build", "zeta")]
    assert not old.exists()
    assert not (artifact / "runtimes/build.toml").exists()
    assert set(path.name for path in artifact.iterdir()) == {"runtimes", "configuration"}
    for name in ["alpha", "zeta"]:
        assert (artifact / f"configuration/runtimes/{name}.json").read_bytes() == (source / f"infrastructure/runtimes/{name}/parameters.json").read_bytes()
    assert (artifact / "configuration/runtime-parameters.json").read_bytes() == (source / "infrastructure/env/dev/runtime-parameters.json").read_bytes()


# 共有コードが全Runtimeのパッケージへ同梱されることを確認します。
# SAMはCodeUriの外にあるファイルを扱わないため、この同梱はbuild.pyの責務です。
# __pycache__のような中間ファイルを持ち込まないことも確認します。
def test_shared_code_is_bundled_into_every_runtime(source, sam):
    (source / "application/common/__pycache__").mkdir()
    (source / "application/common/__pycache__/stale.pyc").write_text("stale")
    artifact = build.build_artifact(source, "dev", "ap-northeast-1")
    for name in ["alpha", "zeta"]:
        bundled = artifact / f"runtimes/{name}/Function/common"
        assert {path.name for path in bundled.iterdir()} == {"__init__.py", "logger.py", "utils.py"}
        assert bundled.joinpath("utils.py").read_bytes() == (source / "application/common/utils.py").read_bytes()


# 共有コードのディレクトリが無い場合は、SAMを呼ぶ前に失敗させます。
# 同梱するものが無いまま不完全なパッケージを作らないためです。
def test_missing_shared_code_stops_before_sam(source, sam):
    shutil.rmtree(source / "application/common")
    with pytest.raises(build.BuildError, match="shared code directory"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []
    assert not (source / "build").exists()


# 持ち運び可能であることの確認。別ディレクトリへコピーし、元のソースを削除してから
# 検証が通ることを確かめます。CodeBuildのDeploy Stageは、これと同じ状況で動きます。
def test_artifact_is_valid_without_original_source(source, sam, tmp_path):
    artifact = build.build_artifact(source, "dev", "ap-northeast-1")
    moved = tmp_path / "relocated"
    shutil.copytree(artifact, moved)
    shutil.rmtree(source)
    build.validate_artifact(moved, {"alpha", "zeta"}, "dev")


# 2件目（zeta）の設定に誤りがある場合、1件目のSAM実行すら始めないことを確認します。
# 時間のかかる処理に入る前に、全件の入力を検証する設計だからです。
def test_invalid_later_parameters_stop_before_any_sam_call(source, sam):
    (source / "infrastructure/runtimes/zeta/parameters.json").write_text('{"Unknown":"value"}')
    (source / "build").mkdir()
    (source / "build/old.txt").write_text("old")
    with pytest.raises(ParameterError):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []
    assert not (source / "build").exists()


# SAMコマンドが途中で失敗した場合の挙動。
#   - 終了コード9をそのまま伝える
#   - 不完全なbuild/を残さない
#   - validateで失敗したらbuildへ進まない、alphaのbuildで失敗したらzetaへ進まない
@pytest.mark.parametrize("failure", [("validate", "zeta"), ("build", "alpha"), ("build", "zeta")])
def test_sam_failure_stops_and_removes_partial_artifact(source, sam, monkeypatch, failure):
    original_run = sam[1]

    def fail(argv, **kwargs):
        # 指定した組み合わせのときだけ失敗させ、それ以外は通常の代役へ渡します。
        template = Path(argv[argv.index("--template-file") + 1])
        if (argv[1], template.parent.name) == failure:
            raise subprocess.CalledProcessError(9, argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(build.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert exc.value.returncode == 9
    assert not (source / "build").exists()
    if failure[0] == "validate":
        assert all(command == "validate" for command, _ in sam[0])
    if failure == ("build", "alpha"):
        assert ("build", "zeta") not in sam[0]


# Source側Templateの未対応・不正な設定を、SAM実行前に拒否することを確認します。
# S3のURI、絶対パス、存在しないパス、組込み関数による指定、境界の外への参照、
# 環境で変わりうるRuntime・Architectures、コンテナImage形式が対象です。
@pytest.mark.parametrize("property_name,value", [
    ("CodeUri", "s3://bucket/key"), ("CodeUri", "/tmp"),
    ("CodeUri", "missing"), ("CodeUri", {"Ref": "Path"}),
    ("CodeUri", "../../../"), ("Runtime", {"Ref": "Runtime"}),
    ("Architectures", {"Ref": "Architecture"}), ("PackageType", "Image"),
])
def test_unsupported_source_build_properties_fail_before_sam(source, sam, property_name, value):
    path = source / "infrastructure/runtimes/zeta/template.yaml"
    template = load_template(path)
    template["Resources"]["Function"]["Properties"][property_name] = value
    path.write_text(yaml.safe_dump(template))
    with pytest.raises(build.BuildError):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []
    assert not (source / "build").exists()


# 必須ファイルの欠落もSAM実行前に検出します。
@pytest.mark.parametrize("missing", ["template.yaml", "parameters.json"])
def test_missing_source_file_stops_before_sam(source, sam, missing):
    (source / "infrastructure/runtimes/zeta" / missing).unlink()
    with pytest.raises(ParameterError):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []


# Runtime名はStack名を通じてResource名の一部になります。
# 上限（20文字）ちょうどは通し、1文字超えたらsamを呼ぶ前に失敗させます。
def test_runtime_name_length_is_limited_before_sam(source, sam):
    runtimes = source / "infrastructure/runtimes"
    (runtimes / "alpha").rename(runtimes / ("a" * build.MAX_RUNTIME_NAME_LENGTH))
    build.build_artifact(source, "dev", "ap-northeast-1")
    sam[0].clear()
    (runtimes / "zeta").rename(runtimes / ("z" * (build.MAX_RUNTIME_NAME_LENGTH + 1)))
    with pytest.raises(build.BuildError, match="at most 20 characters"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []


# bootstrap / ci は、Stack名がBootstrap / CI Stackと同じになるため予約しています。
@pytest.mark.parametrize("name", ["bootstrap", "ci"])
def test_reserved_runtime_names_are_rejected_before_sam(source, sam, name):
    runtimes = source / "infrastructure/runtimes"
    (runtimes / "zeta").rename(runtimes / name)
    with pytest.raises(build.BuildError, match="not bootstrap or ci"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []


# 対象0件は「成功」ではなく設定ミスとして失敗させます。
# 何も配備されないまま処理が成功したように見えることを防ぐためです。
def test_empty_source_fails(source, sam):
    for directory in (source / "infrastructure/runtimes").iterdir():
        shutil.rmtree(directory)
    with pytest.raises(build.BuildError, match="no runtimes"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []


# build/がシンボリックリンクだった場合に、リンク先を消してしまわないことを確認します。
# 出力先を空にする処理が、意図しないディレクトリを削除する事故を防ぎます。
def test_output_symlink_is_not_followed_or_deleted(source, sam, tmp_path):
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "important.txt").write_text("keep")
    (source / "build").symlink_to(outside, target_is_directory=True)
    with pytest.raises(build.BuildError, match="refusing to clear"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert (outside / "important.txt").read_text() == "keep"
    assert sam[0] == []


# ソースコード内のシンボリックリンクも、ビルド前に拒否します。
# Artifactとして持ち運んだときに解釈が変わることを避けるためです。
def test_source_code_symlink_is_rejected_before_sam(source, sam, tmp_path):
    (source / "application/functions/sample_batch_a/link").symlink_to(tmp_path)
    with pytest.raises(build.BuildError, match="source code symlinks"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []


# SAMの生成物が壊れていた場合の確認。生成TemplateのCodeUriを不正な値へ書き換え、
# 生成後の検証で気付いてbuild/を削除することを確かめます。
@pytest.mark.parametrize("uri", ["missing", "../../configuration", "/tmp", "s3://bucket/key"])
def test_bad_generated_code_uri_removes_artifact(source, sam, monkeypatch, uri):
    original_run = sam[1]

    def corrupt(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if argv[1] == "build":
            path = Path(argv[argv.index("--build-dir") + 1]) / "template.yaml"
            template = load_template(path)
            template["Resources"]["Function"]["Properties"]["CodeUri"] = uri
            path.write_text(yaml.safe_dump(template))
        return result

    monkeypatch.setattr(build.subprocess, "run", corrupt)
    with pytest.raises(build.BuildError):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert not (source / "build").exists()


# Artifact検証の確認。正常に作った成果物をあとから壊し、
# 欠落・余剰・環境不一致・シンボリックリンクをそれぞれ検出できることを確かめます。
# この関数はDeploy側でも使うため、部分デプロイを防ぐ要になります。
@pytest.mark.parametrize("damage", ["runtime", "parameter", "extra", "environment", "symlink"])
def test_artifact_validation_rejects_missing_extra_or_mismatched_content(source, sam, damage, tmp_path):
    artifact = build.build_artifact(source, "dev", "ap-northeast-1")
    if damage == "runtime":
        shutil.rmtree(artifact / "runtimes/zeta")                     # Runtimeの欠落
    elif damage == "parameter":
        (artifact / "configuration/runtimes/zeta.json").unlink()      # 設定ファイルの欠落
    elif damage == "extra":
        (artifact / "configuration/runtimes/extra.json").write_text("{}")  # 余分な設定
    elif damage == "environment":
        # dev向けArtifactにprodの環境ファイルが入っている状態
        (artifact / "configuration/runtime-parameters.json").write_text('{"parameters":{"Environment":"prod"},"runtimes":{}}')
    else:
        (artifact / "runtimes/alpha/Function/link").symlink_to(tmp_path)
    with pytest.raises((build.BuildError, ParameterError)):
        build.validate_artifact(artifact, {"alpha", "zeta"}, "dev")


# CLIとして実行した場合、SAMの終了コードをそのまま返すことを確認します。
# CodeBuildはこの終了コードを見てStageの成否を判断します。
def test_cli_propagates_sam_exit_code(source, sam, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(7, args[0])

    monkeypatch.setattr(build.subprocess, "run", fail)
    monkeypatch.chdir(source)   # build_artifactはカレントディレクトリを基準にします
    assert build.main(["--environment", "dev", "--region", "ap-northeast-1"]) == 7
    assert not (source / "build").exists()


# 1 Runtime Stackは1つのデプロイ単位です。Step Functionsのジョブのように
# 複数の関数を持つ場合、すべてをビルドし、共有コードをそれぞれへ同梱します。
def test_multiple_functions_in_one_runtime_are_all_built(source, sam):
    path = source / "infrastructure/runtimes/alpha/template.yaml"
    template = load_template(path)
    function = template["Resources"]["Function"]
    # 2つ目の関数を同じTemplateへ足します。コードの置き場は同じで構いません。
    template["Resources"]["SecondFunction"] = copy.deepcopy(function)
    path.write_text(yaml.safe_dump(template))

    artifact = build.build_artifact(source, "dev", "ap-northeast-1")

    for logical_id in ("Function", "SecondFunction"):
        code = artifact / "runtimes/alpha" / logical_id
        assert (code / "handler.py").is_file()
        assert {item.name for item in (code / "common").iterdir()} == {
            "__init__.py", "logger.py", "utils.py"
        }
    # 関数が1つのRuntimeはこれまでどおりです。
    assert not (artifact / "runtimes/zeta/SecondFunction").exists()


# 関数が1つも無いTemplateは、配備対象が無いまま成功したように見えるため失敗させます。
def test_runtime_without_any_function_fails(source, sam):
    path = source / "infrastructure/runtimes/zeta/template.yaml"
    template = load_template(path)
    template["Resources"] = {
        name: item for name, item in template["Resources"].items()
        if item.get("Type") != "AWS::Serverless::Function"
    }
    path.write_text(yaml.safe_dump(template))
    with pytest.raises(build.BuildError, match="at least one SAM function"):
        build.build_artifact(source, "dev", "ap-northeast-1")
    assert sam[0] == []
