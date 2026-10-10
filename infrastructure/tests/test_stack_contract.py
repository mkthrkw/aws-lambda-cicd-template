# Bootstrap、CI、Runtime、Buildspec、Deploy Script間の受け渡しを横断確認します。
# 個別ファイルの構造テストが成功しても、名前の食い違いでPipelineが失敗するため、
# StackとStageをまたぐ公開契約だけをここで固定します。

import json
from pathlib import Path
import re

import pytest
import yaml

from deployment.scripts import deploy
from deployment.scripts.parameters import load_json, load_template


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "infrastructure/bootstrap/template.yaml"
CI = ROOT / "infrastructure/ci/template.yaml"
RUNTIME = ROOT / "infrastructure/runtimes/sample-batch-a/template.yaml"
ENVIRONMENTS = ("dev", "stg", "prod")


def setup_parameter_file(environment):
    return ROOT / f"infrastructure/env/{environment}/setup-parameters.json"


def setup_parameters(environment):
    # CloudFormationのParameterファイル形式です。`aws cloudformation deploy`が
    # `--parameter-overrides file://` でそのまま読みます。
    entries = json.loads(setup_parameter_file(environment).read_text(encoding="utf-8"))
    assert all(set(entry) == {"ParameterKey", "ParameterValue"} for entry in entries)
    values = {entry["ParameterKey"]: entry["ParameterValue"] for entry in entries}
    assert len(values) == len(entries)
    return values


def project(document, phase):
    return document["Resources"][f"{phase}Project"]["Properties"]


def environment_variables(document, phase):
    return {
        item["Name"]: item["Value"]
        for item in project(document, phase)["Environment"].get("EnvironmentVariables", [])
    }


def test_bootstrap_output_reaches_the_deploy_script():
    bootstrap = load_template(BOOTSTRAP)
    ci = load_template(CI)
    export_name = {"!Sub": "${ProjectName}-${Environment}-bootstrap-RuntimeExecutionRoleArn"}
    output = bootstrap["Outputs"]["RuntimeExecutionRoleArn"]
    assert output["Value"] == {"!GetAtt": "ExecutionRole.Arn"}
    # ARNを人が転記しないよう、CI StackはExportを取り込みます。
    assert output["Export"]["Name"] == export_name
    assert "RuntimeExecutionRoleArn" not in ci["Parameters"]
    assert environment_variables(ci, "Deploy")["CFN_EXECUTION_ROLE_ARN"] == {
        "Fn::ImportValue": export_name
    }
    assert "CFN_EXECUTION_ROLE_ARN" in deploy.REQUIRED_DEPLOY_ENVIRONMENT_VARIABLES


def test_ci_supplies_every_deploy_setting_without_git_stored_account_values():
    ci = load_template(CI)
    supplied = environment_variables(ci, "Deploy")
    # AWS_REGIONはCodeBuildの組込み値なのでTemplate内で上書きしません。
    assert set(supplied) | {"AWS_REGION"} == set(
        deploy.REQUIRED_DEPLOY_ENVIRONMENT_VARIABLES
    )
    assert supplied["EXPECTED_ACCOUNT_ID"] == {"!Ref": "AWS::AccountId"}
    assert supplied["DEPLOYMENT_BUCKET"] == {
        "Fn::ImportValue": {"!Sub": "${ProjectName}-${Environment}-bootstrap-DeploymentBucketName"}
    }
    assert supplied["OPERATIONS_TOPIC_ARN"] == {"!Ref": "OperationsTopic"}
    assert "AccountId" not in ci["Parameters"]
    assert "ArtifactBucketName" not in ci["Parameters"]


def test_operations_topic_is_injected_at_deploy_instead_of_saved_in_environment_json():
    ci = load_template(CI)
    runtime = load_template(RUNTIME)
    assert ci["Outputs"]["OperationsTopicArn"]["Value"] == {
        "!Ref": "OperationsTopic"
    }
    assert "AlarmTopicArn" in runtime["Parameters"]
    # Deploy時に必ず注入するため、Git管理のJSONにはキーごと置きません。
    for environment in ENVIRONMENTS:
        configuration = load_json(
            ROOT / f"infrastructure/env/{environment}/runtime-parameters.json"
        )
        assert "AlarmTopicArn" not in configuration["parameters"]


def test_pipeline_artifacts_match_buildspec_directories_and_environment_names():
    ci = load_template(CI)
    # prodだけに作る承認Stageは !If で囲まれているため、ここでは対象外にします。
    stages = {
        stage["Name"]: stage["Actions"][0]
        for stage in ci["Resources"]["Pipeline"]["Properties"]["Stages"]
        if "!If" not in stage
    }
    assert stages["Source"]["OutputArtifacts"] == [{"Name": "SourceArtifact"}]
    assert stages["Build"]["InputArtifacts"] == [{"Name": "SourceArtifact"}]
    assert stages["Build"]["OutputArtifacts"] == [{"Name": "BuildArtifact"}]
    assert stages["Deploy"]["InputArtifacts"] == [
        {"Name": "SourceArtifact"},
        {"Name": "BuildArtifact"},
    ]
    assert stages["Deploy"]["Configuration"]["PrimarySource"] == "SourceArtifact"

    buildspec = yaml.safe_load(
        (ROOT / "deployment/buildspecs/build.yml").read_text(encoding="utf-8")
    )
    deployspec = yaml.safe_load(
        (ROOT / "deployment/buildspecs/deploy.yml").read_text(encoding="utf-8")
    )
    assert buildspec["artifacts"]["base-directory"] == "build"
    assert set(buildspec["artifacts"]["files"]) == {
        "runtimes/**/*", "configuration/**/*"
    }
    build_command = buildspec["phases"]["build"]["commands"][0]
    assert "${DEPLOY_ENV:?DEPLOY_ENV is required}" in build_command
    assert "${AWS_REGION:?AWS_REGION is required}" in build_command
    deploy_command = deployspec["phases"]["build"]["commands"][0]
    assert "${CODEBUILD_SRC_DIR:?SourceArtifact is required}" in deploy_command
    assert (
        "${CODEBUILD_SRC_DIR_BuildArtifact:?BuildArtifact is required}"
        in deploy_command
    )


def test_each_codebuild_project_points_to_the_tracked_buildspec_for_its_phase():
    ci = load_template(CI)
    for phase in ("Test", "Build", "Deploy"):
        buildspec = project(ci, phase)["Source"]["BuildSpec"]
        assert buildspec == f"deployment/buildspecs/{phase.lower()}.yml"
        assert (ROOT / buildspec).is_file()


# 構築時に手入力する値をなくすため、Bootstrap / CI Stackの入力はGit管理のJSONへ置きます。
# Templateの宣言とJSONのキーがずれると、構築時に初めて失敗します。ここで一致を固定します。
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_setup_parameter_file_matches_the_template_declaration(environment):
    ci = load_template(CI)
    values = setup_parameters(environment)
    declared = set(ci["Parameters"])
    assert set(values) == declared
    assert all(isinstance(value, str) for value in values.values())
    assert values["Environment"] == environment


# Bootstrap StackはCI Stackと同じProjectName・Environmentで作ります。
# 別々に手入力するとStack名の接頭辞が食い違うため、同じファイルから読み出します。
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_bootstrap_parameters_are_covered_by_the_same_file(environment):
    bootstrap = load_template(BOOTSTRAP)
    values = setup_parameters(environment)
    assert set(bootstrap["Parameters"]) <= set(values)


# Templateの制約に合う値だけを置きます。構築前にローカルで気付けるようにするためです。
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_setup_parameter_values_satisfy_the_template_constraints(environment):
    ci = load_template(CI)
    values = setup_parameters(environment)
    for name, value in values.items():
        definition = ci["Parameters"][name]
        if "AllowedPattern" in definition:
            assert re.fullmatch(definition["AllowedPattern"], value), name
        if "AllowedValues" in definition:
            assert value in definition["AllowedValues"], name
        if "MaxLength" in definition:
            assert len(value) <= definition["MaxLength"], name


# 実Account IDやARN、トークンをGitへ置かないための確認です。
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_setup_parameter_file_holds_no_account_identifiers(environment):
    text = setup_parameter_file(environment).read_text(encoding="utf-8")
    assert "arn:" not in text
    assert not re.search(r"\b[0-9]{12}\b", text)


# Deployment BucketとExecution RoleはBootstrap Stackが作り、CI StackはBucket名を取り込みます。
# Export名がずれると、Deployが存在しないBucketへアップロードしようとして失敗します。
def test_ci_imports_the_deployment_bucket_that_bootstrap_exports():
    bootstrap = load_template(BOOTSTRAP)
    ci = load_template(CI)
    export_name = bootstrap["Outputs"]["DeploymentBucketName"]["Export"]["Name"]
    imported = {"Fn::ImportValue": export_name}
    assert environment_variables(ci, "Deploy")["DEPLOYMENT_BUCKET"] == imported
    deploy_policy = {
        item["Sid"]: item
        for policy in ci["Resources"]["DeployRole"]["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
    }
    for sid in ("ReadDeploymentBucket", "ListRuntimeArtifacts", "ManageRuntimeArtifacts"):
        assert deploy_policy[sid]["Resource"]["Fn::Sub"][1] == {"DeploymentBucketName": imported}, sid
    assert "DeployBucket" not in ci["Resources"]


# Execution Roleが読む場所、Deploy Roleが書く場所、SAMがアップロードする場所を一致させます。
def test_runtime_artifact_prefix_matches_between_reader_writer_and_sam():
    bootstrap = load_template(BOOTSTRAP)
    ci = load_template(CI)
    role = bootstrap["Resources"]["ExecutionRole"]["Properties"]
    reader = next(
        item for policy in role["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
        if item["Sid"] == "ReadRuntimeArtifacts"
    )
    assert reader["Resource"] == {"!Sub": "${DeployBucket.Arn}/runtimes/*"}
    writer = next(
        item for policy in ci["Resources"]["DeployRole"]["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
        if item["Sid"] == "ManageRuntimeArtifacts"
    )
    assert writer["Resource"]["Fn::Sub"][0].endswith("/runtimes/*")
    assert '"--s3-prefix", f"runtimes/{stack}"' in Path(deploy.__file__).read_text(encoding="utf-8")

# Stack名とResource名の接頭辞になるため、両TemplateでProjectNameの制約をそろえます。
def test_project_name_constraints_match_between_stacks():
    bootstrap = load_template(BOOTSTRAP)["Parameters"]["ProjectName"]
    ci = load_template(CI)["Parameters"]["ProjectName"]
    for key in ("MinLength", "MaxLength", "AllowedPattern"):
        assert bootstrap[key] == ci[key], key


# Runtime StackのResource名は <Stack名>[-接尾辞] で、Stack名は <ProjectName>-<Environment>-<Runtime名> です。
# BootstrapのExecution Roleが許可する名前の範囲 <ProjectName>-<Environment>-* と一致させます。
# IAM Roleだけは、名前ではなくPath /<ProjectName>/<Environment>/runtime/ で範囲を決めます。
def test_runtime_resource_names_fall_within_the_bootstrap_scope():
    assert 'stack = f"{settings.project}-{settings.environment}-{runtime}"' in Path(
        deploy.__file__
    ).read_text(encoding="utf-8")
    bootstrap = load_template(BOOTSTRAP)
    statements = {
        item["Sid"]: item
        for policy in bootstrap["Resources"]["ExecutionRole"]["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
    }
    expected_suffix = {
        "ManageRuntimeLambda": "function:${ProjectName}-${Environment}-*",
        "ManageRuntimeRoles": "role/${ProjectName}/${Environment}/runtime/*",
        "PassRuntimeRolesToServices": "role/${ProjectName}/${Environment}/runtime/*",
        "ManageRuntimeAlarms": "alarm:${ProjectName}-${Environment}-*",
        "ManageRuntimeSchedules": "schedule/default/${ProjectName}-${Environment}-*",
    }
    for sid, suffix in expected_suffix.items():
        resources = statements[sid]["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        assert any(item["!Sub"].endswith(suffix) for item in resources), sid


# Bootstrap StackのExecution Roleが操作できるIAM Roleは /<ProjectName>/<Environment>/runtime/ だけです。
# Runtime StackのRoleはすべてこのPathに置き、Bootstrap / CI StackのRoleは別のPathに置きます。
# これにより、Runtime TemplateからCIのDeploy RoleやExecution Role自身の権限を変更できません。
def test_only_runtime_roles_use_the_path_managed_by_the_execution_role():
    runtime_path = {"!Sub": "/${ProjectName}/${Environment}/runtime/"}

    def roles(path):
        return {
            logical_id: resource["Properties"].get("Path")
            for logical_id, resource in load_template(path)["Resources"].items()
            if resource["Type"] == "AWS::IAM::Role"
        }

    def assert_all(path, expected):
        found = roles(path)
        assert found and all(value == expected for value in found.values()), found

    for path in sorted((ROOT / "infrastructure/runtimes").glob("*/template.yaml")):
        assert_all(path, runtime_path)
    assert_all(CI, {"!Sub": "/${ProjectName}/${Environment}/ci/"})
    assert_all(BOOTSTRAP, {"!Sub": "/${ProjectName}/${Environment}/cloudformation/"})


# Runtime TemplateのProjectNameはJSONに書かず、Deploy処理がCI Stackの値を注入します。
def test_project_name_is_injected_into_every_runtime_template():
    source = Path(deploy.__file__).read_text(encoding="utf-8")
    assert '"ProjectName": settings.project' in source
    for path in sorted((ROOT / "infrastructure/runtimes").glob("*/template.yaml")):
        assert "ProjectName" in load_template(path)["Parameters"], path.parent.name
    for environment in ENVIRONMENTS:
        runtime_parameters = load_json(ROOT / f"infrastructure/env/{environment}/runtime-parameters.json")
        assert "ProjectName" not in runtime_parameters["parameters"]


# TagはStackへ付け、CloudFormationが各Resourceへ伝播させます。Templateに個別のTagを
# 書くと、書き漏れたResourceだけTagが欠ける状態になるため、どのTemplateにも書きません。
def test_no_template_defines_its_own_resource_tags():
    templates = [BOOTSTRAP, CI, *sorted((ROOT / "infrastructure/runtimes").glob("*/template.yaml"))]
    for path in templates:
        tagged = [
            logical_id
            for logical_id, resource in load_template(path)["Resources"].items()
            if "Tags" in resource.get("Properties", {})
        ]
        assert not tagged, (path.parent.name, tagged)


# Runtime StackのTagは、Deploy処理が sam deploy へ渡します。
def test_deploy_attaches_the_common_stack_tags():
    source = Path(deploy.__file__).read_text(encoding="utf-8")
    for argument in ('f"Project={settings.project}"', 'f"Environment={settings.environment}"',
                     '"ManagedBy=CloudFormation"'):
        assert argument in source, argument
