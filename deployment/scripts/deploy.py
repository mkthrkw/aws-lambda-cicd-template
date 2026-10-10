"""Validate all Runtime artifacts and the AWS account, then deploy with SAM."""

# このファイルの役割
# ------------------
# CI/CDのDeploy Stageで動き、Buildが作ったBuildArtifactをAWSへ配備します。
# ここでは再ビルドを行いません。配備する資材（Template・コード・Parameter）は
# すべてBuildArtifactから取得し、SourceArtifactからはRuntime名の一覧だけを見ます。
#
# 処理の流れ（事故を防ぐため、実際の配備は最後に回します）
#   1. CIから渡された実行設定（環境変数）の形式を検証
#   2. BuildArtifactを全件検証し、全RuntimeのParameterを解決
#   3. 全Runtime分の sam deploy コマンドを先に組み立て、Stack名の長さも確認
#   4. STSで「今の認証情報が想定のAWSアカウントか」を確認
#   5. Runtime名の辞書順に1つずつ sam deploy を実行
#
# 注意: 複数Stackをまとめた原子的な更新ではありません。途中で失敗すると、
# 以降のRuntimeは配備されず、成功済みのStackも自動では戻りません。

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from deployment.scripts.build import BuildError, validate_artifact
from deployment.scripts.parameters import (
    MAX_PROJECT_NAME_LENGTH,
    ParameterError,
    RUNTIME_NAME,
    is_runtime_name,
    sam_parameter_arguments,
)


REQUIRED_DEPLOY_ENVIRONMENT_VARIABLES = (
    "PROJECT_NAME",
    "DEPLOY_ENV",
    "AWS_REGION",
    "EXPECTED_ACCOUNT_ID",
    "DEPLOYMENT_BUCKET",
    "CFN_EXECUTION_ROLE_ARN",
    "OPERATIONS_TOPIC_ARN",
)


class DeployError(ValueError):
    """Invalid deployment input or account mismatch."""

    # 入力が不正、または配備先アカウントが想定と違うときに送出します。


@dataclass(frozen=True)
class DeploymentSettings:
    # CIからCodeBuildへ渡される実行設定です。アプリのParameterとは分けて扱います。
    # frozen=True にしているのは、検証後に値が書き換わらないようにするためです。
    project: str          # Stack名の接頭辞
    environment: str      # dev / stg / prod
    region: str           # 配備先Region
    account: str          # 配備を許可した12桁のAccount ID
    bucket: str           # SAMが資材をアップロードするS3 Bucket
    execution_role: str   # CloudFormationが実際の変更を行うRoleのARN
    operations_topic: str # Runtime Alarmへ渡す、CI Stackが作成したSNS Topic ARN

    @classmethod
    def from_environment(cls, values):
        # 環境変数から設定を読み込みます。1つでも未設定・空文字なら開始しません。
        for key in REQUIRED_DEPLOY_ENVIRONMENT_VARIABLES:
            if not values.get(key):
                raise DeployError(f"{key} is required")
        return cls(*(values[key] for key in REQUIRED_DEPLOY_ENVIRONMENT_VARIABLES))

    def validate(self) -> str:
        # 各値の形式を確認し、AWSのPartition（aws / aws-cn / aws-us-gov）を返します。
        if not RUNTIME_NAME.fullmatch(self.project) or len(self.project) > MAX_PROJECT_NAME_LENGTH:
            raise DeployError("invalid PROJECT_NAME")
        if self.environment not in ("dev", "stg", "prod"):
            raise DeployError("invalid DEPLOY_ENV")
        if not re.fullmatch(r"(?:[a-z]+-)+[0-9]+", self.region):  # 例: ap-northeast-1
            raise DeployError("invalid AWS_REGION")
        if not re.fullmatch(r"[0-9]{12}", self.account):
            raise DeployError("EXPECTED_ACCOUNT_ID must be 12 digits")
        # S3 Bucket名の規則（小文字・数字・ドット・ハイフン、連続ドット禁止）を確認します。
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket) or ".." in self.bucket:
            raise DeployError("invalid DEPLOYMENT_BUCKET name")
        # Regionの接頭辞からPartitionを決めます（中国リージョンや政府向けは別Partition）。
        partition = "aws-cn" if self.region.startswith("cn-") else (
            "aws-us-gov" if self.region.startswith("us-gov-") else "aws"
        )
        role = re.fullmatch(r"arn:(aws|aws-cn|aws-us-gov):iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]+)",
                            self.execution_role)
        # RoleのARNが、想定Account・想定Partitionのものであることまで確認します。
        # 別アカウントのRoleを指したまま配備してしまう事故を防ぎます。
        if not role or role[1] != partition or role[2] != self.account:
            raise DeployError("CloudFormation execution role must match target account and partition")
        topic = re.fullmatch(
            r"arn:(aws|aws-cn|aws-us-gov):sns:([^:]+):([0-9]{12}):([A-Za-z0-9_-]+)",
            self.operations_topic,
        )
        expected_topic = f"{self.project}-{self.environment}-operations"
        if (not topic or topic[1] != partition or topic[2] != self.region
                or topic[3] != self.account or topic[4] != expected_topic):
            raise DeployError("operations topic must match target account, region, project, and environment")
        return partition


def deploy_artifact(source_root: Path, artifact: Path, settings: DeploymentSettings) -> None:
    """Preflight every Runtime before STS and deploy sequentially without rebuilding."""
    partition = settings.validate()
    source_root = source_root.resolve()
    artifact = artifact.resolve()
    # SourceArtifact側のRuntime一覧を取得します（存在確認と命名規則のみ）。
    source_runtimes = list((source_root / "infrastructure/runtimes").iterdir())
    if not source_runtimes or any(
        not path.is_dir() or path.is_symlink() or not is_runtime_name(path.name)
        or not (path / "template.yaml").is_file() for path in source_runtimes
    ):
        raise DeployError("source must contain a nonempty set of Runtime templates")
    runtime_names = {path.name for path in source_runtimes}
    # Only the source names are read. All deployable content comes from BuildArtifact.
    # Source側から読むのは「名前」だけです。成果物の欠落・余剰を検出する用途に限ります。
    resolved = validate_artifact(artifact, runtime_names, settings.environment)
    # Project名と通知Topicの実ARNはGit管理のRuntime用JSONへ書かず、CI Stackから実行時に注入します。
    # ProjectNameはIAM RoleのPath（/<ProjectName>/<Environment>/runtime/）に使います。
    # すべてのRuntimeがこれらのParameterを宣言していることも、Deploy前に確認します。
    injected = {"ProjectName": settings.project, "AlarmTopicArn": settings.operations_topic}
    for runtime, parameters in resolved.items():
        for key, value in injected.items():
            if key not in parameters:
                raise DeployError(f"{runtime}: {key} parameter is required")
            parameters[key] = value
    # 1回目の sam deploy を実行する前に、全Runtime分のコマンドを組み立てておきます。
    # 途中でコマンド組み立てに失敗して部分デプロイになるのを防ぐためです。
    commands = []
    for runtime in sorted(runtime_names):
        # Stack名は <プロジェクト>-<環境>-<Runtime名> に固定します（任意指定は不可）。
        stack = f"{settings.project}-{settings.environment}-{runtime}"
        command = [
            "sam", "deploy", "--template-file", str(artifact / "runtimes" / runtime / "template.yaml"),
            "--stack-name", stack, "--region", settings.region,
            # 資材のアップロード先。実行IDを付けず、同じ場所へ上書きできる形にします。
            "--s3-bucket", settings.bucket, "--s3-prefix", f"runtimes/{stack}",
            # 実際のResource変更はCodeBuildのRoleではなくExecution Roleが行います。
            "--role-arn", settings.execution_role, "--capabilities", "CAPABILITY_NAMED_IAM",
            # 対話確認なしで実行し、変更が無いStackは成功として扱います。
            "--no-confirm-changeset", "--no-fail-on-empty-changeset",
            # StackへTagを付けます。CloudFormationが、Tagに対応するResourceへ自動で伝播します。
            # Resourceごとに書かないのは、書き漏れを防ぎ、Runtimeを追加しても同じTagが付くためです。
            "--tags", f"Project={settings.project}", f"Environment={settings.environment}",
            "ManagedBy=CloudFormation",
            # 解決済みParameterを毎回すべて明示的に渡します（前回値には依存しません）。
            *sam_parameter_arguments(resolved[runtime]),
        ]
        commands.append((runtime, stack, command))

    # 現在の認証情報が「どのAccountのものか」をSTSに問い合わせます。
    identity = subprocess.run([
        "aws", "sts", "get-caller-identity", "--region", settings.region,
        "--output", "json", "--no-cli-pager",
    ], check=True, capture_output=True, text=True, cwd=source_root)
    try:
        caller = json.loads(identity.stdout)
    except (ValueError, TypeError) as exc:
        raise DeployError("STS returned invalid JSON") from exc
    # 想定したAccount以外へは決して配備しません（本番への誤配備を防ぐ最後の砦）。
    if not isinstance(caller, dict) or caller.get("Account") != settings.account:
        raise DeployError("STS account does not match EXPECTED_ACCOUNT_ID")
    arn = caller.get("Arn", "")
    if not isinstance(arn, str) or not re.fullmatch(
        rf"arn:{partition}:(iam|sts)::{settings.account}:.+", arn
    ):
        raise DeployError("STS identity ARN does not match target account and partition")

    # Ignore any repository samconfig profile or deploy switches. This is not release state.
    # リポジトリに samconfig.toml があっても、その profile や no_execute_changeset 等を
    # 引き継がないよう、一時ディレクトリに空の設定ファイルを作って明示的に指定します。
    # ローカル開発用の設定がCIの挙動を変えてしまうことを防ぎます。
    with tempfile.TemporaryDirectory(prefix="lambda-sam-deploy-") as folder:
        config = Path(folder) / "samconfig.toml"
        config.write_text("version = 0.1\n", encoding="utf-8")
        # Runtime名の辞書順に1つずつ配備し、各Stackの完了を待ちます。
        # check=True なので、最初の失敗で例外となり以降は実行されません。
        for runtime, stack, command in commands:
            print(f"{runtime}: deploying {stack}", flush=True)
            subprocess.run([*command, "--config-file", str(config)], cwd=artifact, check=True)
            print(f"{runtime}: deployment succeeded", flush=True)


def main(argv=None) -> int:
    # コマンドラインからの入口です。実行設定は環境変数から受け取ります。
    #   python -m deployment.scripts.deploy --source-dir . --artifact-dir build
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path.cwd())
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        settings = DeploymentSettings.from_environment(os.environ)
        deploy_artifact(args.source_dir, args.artifact_dir, settings)
    except subprocess.CalledProcessError as exc:
        # sam / aws コマンドが失敗した場合。どこまで進んだかが分かるよう明記します。
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        print(f"{exc.cmd[0]} command failed (exit {exc.returncode}); remaining runtimes were not deployed",
              file=sys.stderr)
        return exc.returncode if 0 < exc.returncode < 256 else 1
    except (DeployError, BuildError, ParameterError, OSError) as exc:
        print(f"Deploy failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
