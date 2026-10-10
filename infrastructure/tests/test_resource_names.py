# すべてのTemplateについて、Resource名がAWSの上限の途中で切れないことを確認します。
#
# 名前を指定しないResourceには、CloudFormationが <Stack名>-<論理ID>-<乱数> の名前を付けます。
# 上限を超えると途中が切り詰められ（例: ...-bootst-deploymentbucket-...）、
# 名前からStackを判別しにくくなり、Stack名で権限を絞るIAM Policyからも外れる恐れがあります。
# ProjectNameとRuntime名を上限いっぱいにした、最も長いStack名で確認します。

from pathlib import Path
import re

import pytest

from deployment.scripts.parameters import (
    MAX_PROJECT_NAME_LENGTH,
    MAX_RUNTIME_NAME_LENGTH,
    is_runtime_name,
    load_template,
)


ROOT = Path(__file__).resolve().parents[2]  # リポジトリルート
PROJECT = "p" * MAX_PROJECT_NAME_LENGTH
ENVIRONMENT = "prod"  # dev / stg / prodのうち最長
# 生成名の乱数部分。実環境では12文字でしたが、仕様として明記されていないため1文字余裕をみます。
RANDOM_SUFFIX_LENGTH = 13

# (Template, そのTemplateで最も長くなるStack名)
#
# Runtime Stackは、ProjectNameを上限の20文字にしたうえで、実際のRuntime名を使います。
# 1つのRuntimeに複数の関数がある場合、Resource名には <Stack名>-<ステップ名>-<用途> のように
# 接尾辞が重なります。Runtime名を長くすると上限を超えるため、名前はTemplateごとに判定します。
# Runtime名そのものの上限（20文字）は is_runtime_name が確認します。
TEMPLATES = [
    (ROOT / "infrastructure/bootstrap/template.yaml", f"{PROJECT}-{ENVIRONMENT}-bootstrap"),
    (ROOT / "infrastructure/ci/template.yaml", f"{PROJECT}-{ENVIRONMENT}-ci"),
    *[
        (path, f"{PROJECT}-{ENVIRONMENT}-{path.parent.name}")
        for path in sorted((ROOT / "infrastructure/runtimes").glob("*/template.yaml"))
    ],
]

# 名前を持つResourceの種類と、名前のProperty、名前の上限文字数
NAME_LIMITS = {
    "AWS::S3::Bucket": ("BucketName", 63),
    "AWS::IAM::Role": ("RoleName", 64),
    "AWS::Events::Rule": ("Name", 64),
    "AWS::Serverless::Function": ("FunctionName", 64),
    "AWS::Scheduler::Schedule": ("Name", 64),
    "AWS::StepFunctions::StateMachine": ("StateMachineName", 80),
    "AWS::SQS::Queue": ("QueueName", 80),
    "AWS::CloudWatch::Alarm": ("AlarmName", 255),
    "AWS::Logs::LogGroup": ("LogGroupName", 512),
    "AWS::CodeBuild::Project": ("Name", 255),
    "AWS::CodePipeline::Pipeline": ("Name", 100),
    "AWS::SNS::Topic": ("TopicName", 256),
    "AWS::CodeConnections::Connection": ("ConnectionName", 32),
}
# 名前を持たないResource。新しい種類を追加したら、どちらかへ分類します。
UNNAMED_TYPES = {"AWS::S3::BucketPolicy", "AWS::SNS::Subscription", "AWS::SNS::TopicPolicy"}
# SAMがDeploymentPreferenceから生成するCodeDeployのResource。名前の上限は100文字です。
CODEDEPLOY_NAME_LIMIT = 100


def generated_length(stack, logical_id):
    return len(stack) + 1 + len(logical_id) + 1 + RANDOM_SUFFIX_LENGTH


def fixed_length(value, stack):
    # !Sub の中の変数を、最も長い値に置き換えた長さを返します。
    values = {"ProjectName": PROJECT, "Environment": ENVIRONMENT, "AWS::StackName": stack}
    return len(re.sub(r"\$\{([^}]+)\}", lambda match: values[match[1]], value))


@pytest.mark.parametrize("path,stack", TEMPLATES, ids=[path.parent.name for path, _ in TEMPLATES])
def test_resource_names_fit_their_limits_without_truncation(path, stack):
    for logical_id, resource in load_template(path)["Resources"].items():
        resource_type = resource["Type"]
        if resource_type in UNNAMED_TYPES:
            continue
        assert resource_type in NAME_LIMITS, f"{logical_id}: classify {resource_type}"
        name_property, limit = NAME_LIMITS[resource_type]
        name = resource.get("Properties", {}).get(name_property)
        if name is None:
            length = generated_length(stack, logical_id)
        else:
            assert isinstance(name, dict) and set(name) == {"!Sub"}, logical_id
            length = fixed_length(name["!Sub"], stack)
        assert length <= limit, f"{logical_id}: {length} > {limit}"

        if resource_type == "AWS::Serverless::Function" and "DeploymentPreference" in resource["Properties"]:
            assert generated_length(stack, "ServerlessDeploymentApplication") <= CODEDEPLOY_NAME_LIMIT
            assert generated_length(stack, f"{logical_id}DeploymentGroup") <= CODEDEPLOY_NAME_LIMIT


# 上の確認は、Templateと Build / Deploy処理が同じ上限を使っていることが前提です。
def test_project_name_limit_is_shared_by_scripts_and_templates():
    for path, _ in TEMPLATES[:2]:
        assert load_template(path)["Parameters"]["ProjectName"]["MaxLength"] == MAX_PROJECT_NAME_LENGTH


# Resource名の判定は実際のRuntime名で行うため、Runtime名自体が規則内であることも確かめます。
def test_runtime_names_follow_the_rule():
    names = [path.parent.name for path in (ROOT / "infrastructure/runtimes").glob("*/template.yaml")]
    assert names
    assert all(is_runtime_name(name) for name in names), names
    assert all(len(name) <= MAX_RUNTIME_NAME_LENGTH for name in names)
