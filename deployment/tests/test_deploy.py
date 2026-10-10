# deployment/scripts/deploy.py のUnit Test。
#
# 重要な前提: AWSへは接続しません。
# monkeypatchでsubprocess.runを差し替え、aws sts get-caller-identityとsam deployを
# 代役に置き換えます。確認したいのは「どのコマンドが、どの引数で、何件呼ばれたか」と、
# 「不正な入力のときに1件も呼ばれないこと」です。
#
# 部分デプロイ（一部のStackだけ更新されてしまう状態）を防げているかが、
# このファイルの中心的な関心事です。

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from deployment.scripts import deploy
from deployment.scripts.build import BuildError
from deployment.scripts.parameters import MAX_RUNTIME_NAME_LENGTH, ParameterError, load_template

ROOT = Path(__file__).resolve().parents[2]  # リポジトリルート


@pytest.fixture
def settings():
    # CIから渡される実行設定の正常値。テスト用のダミーAccount IDを使います。
    return deploy.DeploymentSettings("sample", "dev", "ap-northeast-1", "123456789012",
                                     "sample-deployment-bucket",
                                     "arn:aws:iam::123456789012:role/service/cfn-execution",
                                     "arn:aws:sns:ap-northeast-1:123456789012:sample-dev-operations")


@pytest.fixture
def inputs(tmp_path):
    # Deployの2つの入力（SourceArtifactとBuildArtifact）を作ります。
    # Source側のtemplate.yamlとparameters.jsonには、あえて使ってはいけない内容を入れています。
    # Deployがこれらを読んでしまうと、テストが失敗する仕掛けです。
    source = tmp_path / "source with spaces"
    artifact = tmp_path / "artifact with spaces"
    for name in ["zeta", "beta", "alpha"]:
        original = source / "infrastructure/runtimes" / name
        original.mkdir(parents=True)
        (original / "template.yaml").write_text("must not be read as deploy input")
        (original / "parameters.json").write_text('{"Unknown":"must-not-use"}')
        # Artifact側は、sam buildの生成物と同じ形（CodeUriが相対パス）にします。
        destination = artifact / "runtimes" / name
        (destination / "Function").mkdir(parents=True)
        (destination / "Function/handler.py").write_text("# built code")
        template = load_template(ROOT / "infrastructure/runtimes/sample-batch-a/template.yaml")
        template["Resources"]["Function"]["Properties"]["CodeUri"] = "Function"
        (destination / "template.yaml").write_text(yaml.safe_dump(template))
    configuration = artifact / "configuration"
    (configuration / "runtimes").mkdir(parents=True)
    for name in ["alpha", "beta", "zeta"]:
        (configuration / f"runtimes/{name}.json").write_text("{}")
    # betaだけLogRetentionDaysを30に上書きし、Runtime個別値が効くことを確認できるようにします。
    (configuration / "runtime-parameters.json").write_text(json.dumps({
        "parameters": {"Environment": "dev", "AlarmTopicArn": ""},
        "runtimes": {"beta": {"LogRetentionDays": "30"}},
    }))
    return source, artifact


@pytest.fixture
def commands(monkeypatch, settings):
    # AWS CLIとSAM CLIの代役。呼び出しをcallsへ記録します。
    calls = []

    def run(argv, **kwargs):
        # シェルを介さず、失敗を検出する設定で呼ばれていることを確認します。
        assert kwargs["check"] is True
        assert "shell" not in kwargs
        calls.append((argv, kwargs))
        if argv[0] == "aws":
            # STSの応答を模擬します。想定どおりのAccountを返す正常系です。
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "Account": settings.account,
                "Arn": f"arn:aws:sts::{settings.account}:assumed-role/build/session",
            }))
        assert argv[:2] == ["sam", "deploy"]
        # 一時的な空のsamconfigを指定していることを確認します。
        # リポジトリのsamconfig.tomlの設定を引き継がないための仕組みです。
        config = Path(argv[argv.index("--config-file") + 1])
        assert config.read_text() == "version = 0.1\n"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    return calls, run


# 正常系。次を確認します。
#   - 最初にSTSでAccountを確認し、そのあと3件のsam deployを実行する（合計4回）
#   - Template・Stack名・prefix・Role・Regionが想定どおり
#   - 解決済みParameterを毎回すべて渡す。betaだけ30日になる
#   - 共通TagをStackへ渡す
#   - 作業ディレクトリがArtifact側で、一時samconfigは終了後に消えている
#   - Artifactの中身を1バイトも書き換えない
def test_deploy_uses_only_built_content_and_all_parameters_in_order(inputs, settings, commands):
    source, artifact = inputs
    before = {str(p.relative_to(artifact)): p.read_bytes() for p in artifact.rglob("*") if p.is_file()}
    deploy.deploy_artifact(source, artifact, settings)
    calls = commands[0]
    assert calls[0][0][:3] == ["aws", "sts", "get-caller-identity"]
    assert len(calls) == 4
    # Runtime名の辞書順（alpha → beta → zeta）で処理されることも、この並びで確認しています。
    for name, (argv, options) in zip(["alpha", "beta", "zeta"], calls[1:]):
        assert argv[argv.index("--template-file") + 1] == str(artifact / f"runtimes/{name}/template.yaml")
        assert argv[argv.index("--stack-name") + 1] == f"sample-dev-{name}"
        assert argv[argv.index("--s3-prefix") + 1] == f"runtimes/sample-dev-{name}"
        assert argv[argv.index("--role-arn") + 1] == settings.execution_role
        # Runtime TemplateはIAM Roleに名前を付けるため、CAPABILITY_NAMED_IAMが必要です。
        assert argv[argv.index("--capabilities") + 1] == "CAPABILITY_NAMED_IAM"
        assert argv[argv.index("--region") + 1] == settings.region
        assert "--no-fail-on-empty-changeset" in argv
        assert "--no-confirm-changeset" in argv
        # TagはStackへ付け、CloudFormationが各Resourceへ伝播させます。
        tags = argv[argv.index("--tags") + 1:argv.index("--tags") + 4]
        assert tags == ["Project=sample", "Environment=dev", "ManagedBy=CloudFormation"]
        assert (
            'ParameterKey=AlarmTopicArn,ParameterValue="'
            'arn:aws:sns:ap-northeast-1:123456789012:sample-dev-operations"'
        ) in argv
        assert 'ParameterKey=Environment,ParameterValue="dev"' in argv
        # ProjectNameはJSONに無く、CI Stackの値（PROJECT_NAME）が注入されます。
        assert 'ParameterKey=ProjectName,ParameterValue="sample"' in argv
        days = "30" if name == "beta" else "14"
        assert f'ParameterKey=LogRetentionDays,ParameterValue="{days}"' in argv
        assert options["cwd"] == artifact
        assert not Path(argv[argv.index("--config-file") + 1]).exists()
    assert before == {str(p.relative_to(artifact)): p.read_bytes() for p in artifact.rglob("*") if p.is_file()}


# 実行設定が不正なら、外部コマンドを1件も実行しません。
# 特に重要なのは最後の3件です。別Account・別Partition・Role以外のARNを
# Execution Roleとして受け付けないことを確認しています。
# regionの"--profile=other"は、値がそのままコマンドのオプションとして
# 解釈されないことの確認です。
@pytest.mark.parametrize("field,value", [
    ("project", "bad_name"), ("project", "x" * 21), ("environment", "other"),
    ("region", "--profile=other"), ("account", "123"), ("bucket", ""),
    ("bucket", "s3://bucket"), ("bucket", "bad..bucket"),
    ("execution_role", "arn:aws:iam::999999999999:role/cfn"),
    ("execution_role", "arn:aws-cn:iam::123456789012:role/cfn"),
    ("execution_role", "arn:aws:sts::123456789012:assumed-role/cfn/session"),
    ("operations_topic", "arn:aws:sns:ap-northeast-1:999999999999:sample-dev-operations"),
    ("operations_topic", "arn:aws-cn:sns:ap-northeast-1:123456789012:sample-dev-operations"),
    ("operations_topic", "arn:aws:sns:us-east-1:123456789012:sample-dev-operations"),
    ("operations_topic", "arn:aws:sns:ap-northeast-1:123456789012:other-dev-operations"),
])
def test_invalid_settings_stop_before_any_external_command(inputs, settings, commands, field, value):
    # replaceはdataclassの一部の項目だけを差し替えた複製を作る関数です。
    with pytest.raises(deploy.DeployError):
        deploy.deploy_artifact(*inputs, replace(settings, **{field: value}))
    assert commands[0] == []


# ProjectNameとRuntime名は、どのResource名も途中で切れない長さに制限します。
# 上限を1文字でも超えたら、STSもsam deployも実行しません。
def test_project_name_longer_than_limit_stops_before_any_external_command(inputs, settings, commands):
    project = "p" * (deploy.MAX_PROJECT_NAME_LENGTH + 1)
    with pytest.raises(deploy.DeployError, match="invalid PROJECT_NAME"):
        deploy.deploy_artifact(*inputs, replace(
            settings, project=project,
            operations_topic=f"arn:aws:sns:ap-northeast-1:123456789012:{project}-dev-operations",
        ))
    assert commands[0] == []


# 予約名（bootstrap、ci）はBootstrap / CI Stackと同じStack名になるため、同様に止めます。
@pytest.mark.parametrize("name", ["z" * (MAX_RUNTIME_NAME_LENGTH + 1), "bootstrap", "ci"])
def test_invalid_runtime_name_stops_before_any_external_command(inputs, settings, commands, name):
    source, artifact = inputs
    runtimes = source / "infrastructure/runtimes"
    (runtimes / "zeta").rename(runtimes / name)
    with pytest.raises(deploy.DeployError, match="Runtime templates"):
        deploy.deploy_artifact(source, artifact, settings)
    assert commands[0] == []


# 3件目（zeta）の資材に問題がある場合でも、STSもsam deployも実行しないことを確認します。
# 1件目・2件目だけが配備されてしまう部分デプロイを防ぐための、最も重要なテストです。
# sourceの欠落（最後のelse）は、Source側とArtifact側のRuntime名の不一致を表します。
@pytest.mark.parametrize("damage", ["parameter", "code", "template", "environment", "extra", "source"])
def test_all_runtime_preflight_before_sts_or_sam(inputs, settings, commands, damage):
    source, artifact = inputs
    if damage == "parameter":
        (artifact / "configuration/runtimes/zeta.json").write_text('{"Unknown":"x"}')
    elif damage == "code":
        (artifact / "runtimes/zeta/Function").rename(artifact / "runtimes/zeta/missing")
    elif damage == "template":
        (artifact / "runtimes/zeta/template.yaml").unlink()
    elif damage == "environment":
        (artifact / "configuration/runtime-parameters.json").write_text('{"parameters":{"Environment":"prod"},"runtimes":{}}')
    elif damage == "extra":
        (artifact / "configuration/runtimes/extra.json").write_text("{}")
    else:
        (source / "infrastructure/runtimes/zeta/template.yaml").unlink()
    with pytest.raises((deploy.DeployError, BuildError, ParameterError)):
        deploy.deploy_artifact(source, artifact, settings)
    assert commands[0] == []


# STSの応答が想定と違う場合、sam deployへ進まないことを確認します。
# 壊れたJSON、Account不一致、ARNの欠落、別PartitionのARNが対象です。
# 呼び出しが1件（STSのみ）であることが、配備されていないことの証拠になります。
@pytest.mark.parametrize("identity", ["invalid JSON", "[]", "{}",
    '{"Account":"999999999999"}', '{"Account":"123456789012"}',
    '{"Account":"123456789012","Arn":"arn:aws-cn:iam::123456789012:root"}',
])
def test_invalid_sts_identity_never_deploys(inputs, settings, monkeypatch, identity):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, identity)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(deploy.DeployError):
        deploy.deploy_artifact(*inputs, settings)
    assert len(calls) == 1
    assert calls[0][0] == "aws"


# 途中で失敗した場合、後続のRuntime（zeta）へ進まないことを確認します。
# 併せて、失敗してもArtifactを消さないことも確かめます。
# Buildと違い、Deployの入力は他Stageの成果物なので後片付けの対象外です。
@pytest.mark.parametrize("fail_at", ["sts", "beta"])
def test_command_failure_stops_and_preserves_artifact(inputs, settings, commands, monkeypatch, fail_at):
    original_run = commands[1]

    def fail(argv, **kwargs):
        if (fail_at == "sts" and argv[0] == "aws") or (fail_at == "beta" and "sample-dev-beta" in argv):
            raise subprocess.CalledProcessError(8, argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(deploy.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy_artifact(*inputs, settings)
    assert not any("sample-dev-zeta" in argv for argv, _ in commands[0])
    assert (inputs[1] / "runtimes/zeta/template.yaml").is_file()


# 必須の環境変数が欠けている場合、どの変数が足りないかを名指しで報告することを確認します。
# 1つずつ値を埋めながら、7つすべてが必須であることを順に確かめています。
def test_missing_environment_settings_are_reported():
    values = {}
    for key in deploy.REQUIRED_DEPLOY_ENVIRONMENT_VARIABLES:
        with pytest.raises(deploy.DeployError, match=key):
            deploy.DeploymentSettings.from_environment(values)
        values[key] = "placeholder"


# CLIとして実行した場合、失敗したコマンドの終了コードをそのまま返すことを確認します。
# CodeBuildはこの終了コードでStageの成否を判断します。
def test_cli_propagates_exit_code(inputs, settings, monkeypatch):
    # 環境変数を用意する代わりに、設定の読込処理ごと固定値へ差し替えます。
    monkeypatch.setattr(deploy.DeploymentSettings, "from_environment", lambda _: settings)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(7, ["aws", "sts", "get-caller-identity"])

    monkeypatch.setattr(deploy.subprocess, "run", fail)
    assert deploy.main(["--source-dir", str(inputs[0]), "--artifact-dir", str(inputs[1])]) == 7
