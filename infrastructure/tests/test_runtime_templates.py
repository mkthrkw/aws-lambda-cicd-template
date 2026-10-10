# Runtime Stack Templateの命名規則を、AWSへ接続せずに確認します。
#
# Runtime Stackの中のResourceは、Bootstrap StackのExecution Roleが作ります。
# そのRoleは <ProjectName>-<Environment>-* という名前のResourceだけを許可します。
# 名前の上限が64文字のResourceを自動生成に任せると途中が切り詰められ、この範囲から
# 外れる恐れがあるため、Stack名 <ProjectName>-<Environment>-<Runtime名> を土台に名前を付けます。
# 名前の長さは test_resource_names.py で確認します。
# 新しいRuntimeを追加したときも同じ規則を守れるよう、全Runtime Templateを対象にします。

from pathlib import Path

import pytest

from deployment.scripts.parameters import load_template


ROOT = Path(__file__).resolve().parents[2]  # リポジトリルート
RUNTIME_TEMPLATES = sorted((ROOT / "infrastructure/runtimes").glob("*/template.yaml"))

# 名前を付けるResourceと、名前を指定するProperty
NAMED_TYPES = {
    "AWS::Serverless::Function": "FunctionName",
    "AWS::IAM::Role": "RoleName",
    "AWS::Scheduler::Schedule": "Name",
    "AWS::CloudWatch::Alarm": "AlarmName",
}


def runtime_ids():
    return [path.parent.name for path in RUNTIME_TEMPLATES]


def test_runtime_templates_exist():
    assert RUNTIME_TEMPLATES


@pytest.mark.parametrize("path", RUNTIME_TEMPLATES, ids=runtime_ids())
def test_named_resources_start_with_the_stack_name(path):
    for logical_id, resource in load_template(path)["Resources"].items():
        name_property = NAMED_TYPES.get(resource["Type"])
        if not name_property:
            continue
        name = resource.get("Properties", {}).get(name_property)
        assert isinstance(name, dict) and "!Sub" in name, f"{logical_id}: {name_property} is required"
        value = name["!Sub"]
        assert value.startswith("${AWS::StackName}"), logical_id


@pytest.mark.parametrize("path", RUNTIME_TEMPLATES, ids=runtime_ids())
def test_retained_log_groups_keep_generated_names(path):
    # Stack削除後も残すLog Groupに固定名を付けると、作り直すときに同じ名前が残っていて失敗します。
    for logical_id, resource in load_template(path)["Resources"].items():
        if resource["Type"] == "AWS::Logs::LogGroup" and resource.get("DeletionPolicy") == "Retain":
            assert "LogGroupName" not in resource.get("Properties", {}), logical_id


@pytest.mark.parametrize("path", RUNTIME_TEMPLATES, ids=runtime_ids())
def test_codedeploy_role_is_defined_instead_of_generated_by_sam(path):
    # DeploymentPreferenceにRoleを指定しないと、SAMが名前を自動生成したRoleを作ります。
    resources = load_template(path)["Resources"]
    for logical_id, resource in resources.items():
        preference = resource.get("Properties", {}).get("DeploymentPreference")
        if resource["Type"] != "AWS::Serverless::Function" or not preference:
            continue
        role = preference.get("Role")
        assert isinstance(role, dict) and "!GetAtt" in role, logical_id
        role_id = role["!GetAtt"].split(".", 1)[0]
        role_resource = resources[role_id]
        assert role_resource["Type"] == "AWS::IAM::Role"
        assert role_resource["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
            "Service": "codedeploy.amazonaws.com"
        }
        assert role_resource["Properties"]["ManagedPolicyArns"] == [
            {"!Sub": "arn:${AWS::Partition}:iam::aws:policy/service-role/AWSCodeDeployRoleForLambda"}
        ]


@pytest.mark.parametrize("path", RUNTIME_TEMPLATES, ids=runtime_ids())
def test_logical_ids_do_not_contain_the_runtime_name(path):
    # 論理IDはStackの中だけの名前です。Runtime名を含めなければ、
    # ディレクトリをコピーして別のRuntimeを作るときにTemplateを書き換えずに済みます。
    template = load_template(path)
    runtime = path.parent.name
    forbidden = {runtime.replace("-", "").lower(), "sample"}
    for logical_id in [*template["Resources"], *template.get("Outputs", {})]:
        assert not any(word in logical_id.lower() for word in forbidden), logical_id
