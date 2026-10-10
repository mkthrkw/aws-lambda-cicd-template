# CI StackのStage接続、IAM境界、Artifact保持、通知経路をAWS未接続で確認します。
# CloudFormation lintでは検出できない「Test RoleにDeploy権限が混ざった」等の
# 事故を防ぐため、設計上の境界をTemplate構造として固定します。

from collections import Counter
from pathlib import Path

import pytest

from deployment.scripts.parameters import load_template


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROOT / "infrastructure/ci/template.yaml"


def template():
    return load_template(TEMPLATE_PATH)


def resource(logical_id):
    return template()["Resources"][logical_id]


def statements(role_id):
    role = resource(role_id)
    return {
        item["Sid"]: item
        for policy in role["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
    }


def actions(role_id):
    result = set()
    for item in statements(role_id).values():
        action = item["Action"]
        result.update(action if isinstance(action, list) else [action])
    return result


def ref(name):
    return {"!Ref": name}


def imported(name):
    # Bootstrap StackがExportした値の取り込みを表します。
    return {"Fn::ImportValue": sub(f"${{ProjectName}}-${{Environment}}-bootstrap-{name}")}


def sub(value):
    return {"!Sub": value}


def get_att(value):
    return {"!GetAtt": value}


def pipeline_stages(*, include_conditional=True):
    # prodだけに作る承認Stageは !If で囲まれています。取り出して同じ形で扱えるようにします。
    result = []
    for item in resource("Pipeline")["Properties"]["Stages"]:
        if "!If" in item:
            condition, when_true, when_false = item["!If"]
            assert condition == "IsProd", condition
            assert when_false == {"!Ref": "AWS::NoValue"}   # dev / stgでは要素ごと消える
            if include_conditional:
                result.append(when_true)
        else:
            result.append(item)
    return result


def test_parameters_and_outputs_are_the_documented_interface():
    document = template()
    parameters = document["Parameters"]
    assert set(parameters) == {
        "ProjectName",
        "Environment",
        "GitHubRepositoryId",
        "BranchName",
    }
    assert "Default" not in parameters["ProjectName"]
    assert "Default" not in parameters["GitHubRepositoryId"]
    assert "Default" not in parameters["BranchName"]
    assert parameters["Environment"]["AllowedValues"] == ["dev", "stg", "prod"]
    # 条件分岐はprod判定だけです。通知の受け取り先はTemplateで管理しません。
    assert document["Conditions"] == {
        "IsProd": {"!Equals": [{"!Ref": "Environment"}, "prod"]}
    }
    assert {
        name: output["Value"] for name, output in document["Outputs"].items()
    } == {
        "GitHubConnectionArn": ref("GitHubConnection"),
        "PipelineName": ref("Pipeline"),
        "ArtifactBucketName": ref("ArtifactBucket"),
        "OperationsTopicArn": ref("OperationsTopic"),
    }


def test_ci_stack_contains_the_expected_resources():
    resources = template()["Resources"]
    assert Counter(item["Type"] for item in resources.values()) == Counter({
        "AWS::CodeConnections::Connection": 1,
        "AWS::S3::Bucket": 1,
        "AWS::S3::BucketPolicy": 1,
        "AWS::Logs::LogGroup": 3,
        "AWS::IAM::Role": 4,
        "AWS::CodeBuild::Project": 3,
        "AWS::CodePipeline::Pipeline": 1,
        "AWS::SNS::Topic": 1,
        "AWS::SNS::TopicPolicy": 1,
        "AWS::Events::Rule": 1,
    })
    for role_id, service in {
        "PipelineRole": "codepipeline.amazonaws.com",
        "TestRole": "codebuild.amazonaws.com",
        "BuildRole": "codebuild.amazonaws.com",
        "DeployRole": "codebuild.amazonaws.com",
    }.items():
        properties = resources[role_id]["Properties"]
        assert "RoleName" not in properties
        assert properties["AssumeRolePolicyDocument"]["Statement"] == [{
            "Sid": "CodePipelineOnly" if role_id == "PipelineRole" else "CodeBuildOnly",
            "Effect": "Allow",
            "Principal": {"Service": service},
            "Action": "sts:AssumeRole",
        }]
    assert "BucketName" not in resources["ArtifactBucket"]["Properties"]
    # Deployment BucketはBootstrap Stackが作ります。CI Stackには置きません。
    assert "DeployBucket" not in resources
    assert "DeployBucketPolicy" not in resources


def test_connection_and_fixed_resource_names_follow_project_environment():
    assert resource("GitHubConnection")["Properties"] == {
        "ConnectionName": sub("${ProjectName}-${Environment}"),
        "ProviderType": "GitHub",
    }
    assert resource("Pipeline")["Properties"]["Name"] == sub(
        "${ProjectName}-${Environment}-pipeline"
    )
    assert resource("OperationsTopic")["Properties"]["TopicName"] == sub(
        "${ProjectName}-${Environment}-operations"
    )
    for phase in ("Test", "Build", "Deploy"):
        suffix = phase.lower()
        assert resource(f"{phase}Project")["Properties"]["Name"] == sub(
            f"${{ProjectName}}-${{Environment}}-{suffix}"
        )
        assert resource(f"{phase}LogGroup")["Properties"] == {
            "LogGroupName": sub(
                f"/aws/codebuild/${{ProjectName}}-${{Environment}}-{suffix}"
            ),
            "RetentionInDays": 30,
        }


@pytest.mark.parametrize("bucket_id", ["ArtifactBucket"])
def test_buckets_are_private_encrypted_versioned_and_retained(bucket_id):
    bucket = resource(bucket_id)
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    properties = bucket["Properties"]
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    assert set(properties["PublicAccessBlockConfiguration"].values()) == {True}
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [{
            "ServerSideEncryptionByDefault": {
                "SSEAlgorithm": "aws:kms",
                "KMSMasterKeyID": "alias/aws/s3",
            }
        }]
    }
    rules = {item["Id"]: item for item in properties["LifecycleConfiguration"]["Rules"]}
    assert rules["AbortIncompleteUploads"]["AbortIncompleteMultipartUpload"] == {
        "DaysAfterInitiation": 7
    }
    deny = [
        item for item in resource(f"{bucket_id}Policy")["Properties"]["PolicyDocument"]["Statement"]
        if item["Sid"] == "DenyInsecureTransport"
    ]
    assert deny == [{
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [get_att(f"{bucket_id}.Arn"), sub(f"${{{bucket_id}.Arn}}/*")],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }]


# CodePipelineはArtifactのキーを「Pipeline名の先頭20文字/Artifact名の先頭10文字/乱数」で作ります。
# 例: lambda-simple-templa/SourceArti/d8ptpz6。Pipeline名そのものをプレフィックスにすると一致しません。
# CloudFormationでは切り詰めた値を作れないため、Pipeline専用Bucketとし、権限と保持期間をBucket全体に適用します。
def test_pipeline_artifact_bucket_does_not_depend_on_the_pipeline_name_prefix():
    rules = {item["Id"]: item for item in
             resource("ArtifactBucket")["Properties"]["LifecycleConfiguration"]["Rules"]}
    assert rules["ExpirePipelineArtifacts"] == {
        "Id": "ExpirePipelineArtifacts",
        "Status": "Enabled",
        "Prefix": "",
        "ExpirationInDays": 30,
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    }
    assert "ExpireRuntimeArtifacts" not in rules
    artifact_object = sub("${ArtifactBucket.Arn}/*")
    for role_id, sid in (("PipelineRole", "TransferPipelineArtifacts"),
                         ("TestRole", "ReadPipelineArtifacts"),
                         ("BuildRole", "TransferPipelineArtifacts"),
                         ("DeployRole", "ReadPipelineArtifacts")):
        assert statements(role_id)[sid]["Resource"] == artifact_object, role_id
    assert "pipeline/" not in (ROOT / "infrastructure/ci/template.yaml").read_text()


# SAMの資材を置くDeployment BucketはBootstrap Stackが作ります。
# CI StackはそのExportからBucket名を取り込み、Deploy Roleの権限を runtimes/ に限定します。
def test_deploy_role_uses_the_imported_deployment_bucket():
    bucket_name = {"Fn::ImportValue": sub("${ProjectName}-${Environment}-bootstrap-DeploymentBucketName")}
    policy = statements("DeployRole")
    assert policy["ReadDeploymentBucket"]["Resource"] == {"Fn::Sub": [
        "arn:${AWS::Partition}:s3:::${DeploymentBucketName}",
        {"DeploymentBucketName": bucket_name},
    ]}
    assert policy["ListRuntimeArtifacts"]["Resource"] == {"Fn::Sub": [
        "arn:${AWS::Partition}:s3:::${DeploymentBucketName}",
        {"DeploymentBucketName": bucket_name},
    ]}
    assert policy["ListRuntimeArtifacts"]["Condition"] == {
        "StringLike": {"s3:prefix": "runtimes/*"}
    }
    assert policy["ManageRuntimeArtifacts"]["Resource"] == {"Fn::Sub": [
        "arn:${AWS::Partition}:s3:::${DeploymentBucketName}/runtimes/*",
        {"DeploymentBucketName": bucket_name},
    ]}
    # Pipeline / Test / Build はSAMの資材に触れません。
    for role_id in ("PipelineRole", "TestRole", "BuildRole"):
        assert "DeploymentBucketName" not in str(statements(role_id)), role_id


def test_codebuild_projects_use_existing_buildspecs_and_isolated_roles():
    expected = {
        "Test": ("CODEPIPELINE", 15, []),
        "Build": ("CODEPIPELINE", 30, [
            {"Name": "DEPLOY_ENV", "Type": "PLAINTEXT", "Value": ref("Environment")}
        ]),
        "Deploy": ("CODEPIPELINE", 60, [
            {"Name": "PROJECT_NAME", "Type": "PLAINTEXT", "Value": ref("ProjectName")},
            {"Name": "DEPLOY_ENV", "Type": "PLAINTEXT", "Value": ref("Environment")},
            {"Name": "EXPECTED_ACCOUNT_ID", "Type": "PLAINTEXT", "Value": ref("AWS::AccountId")},
            {"Name": "DEPLOYMENT_BUCKET", "Type": "PLAINTEXT", "Value": imported("DeploymentBucketName")},
            {"Name": "CFN_EXECUTION_ROLE_ARN", "Type": "PLAINTEXT", "Value": imported("RuntimeExecutionRoleArn")},
            {"Name": "OPERATIONS_TOPIC_ARN", "Type": "PLAINTEXT", "Value": ref("OperationsTopic")},
        ]),
    }
    for phase, (artifact_type, timeout, environment_variables) in expected.items():
        project = resource(f"{phase}Project")["Properties"]
        assert project["ServiceRole"] == get_att(f"{phase}Role.Arn")
        assert project["Source"] == {
            "Type": "CODEPIPELINE",
            "BuildSpec": f"deployment/buildspecs/{phase.lower()}.yml",
        }
        assert project["Artifacts"] == {"Type": artifact_type}
        environment = project["Environment"]
        assert environment["Type"] == "LINUX_CONTAINER"
        assert environment["ComputeType"] == "BUILD_GENERAL1_SMALL"
        assert environment["Image"] == "aws/codebuild/standard:7.0"
        assert environment["ImagePullCredentialsType"] == "CODEBUILD"
        assert environment["PrivilegedMode"] is False
        assert environment.get("EnvironmentVariables", []) == environment_variables
        assert "AWS_REGION" not in {
            item["Name"] for item in environment.get("EnvironmentVariables", [])
        }
        assert project["TimeoutInMinutes"] == timeout
        assert project["QueuedTimeoutInMinutes"] == 60
        assert project["ConcurrentBuildLimit"] == 1
        assert "VpcConfig" not in project
        assert "Cache" not in project
        assert (ROOT / project["Source"]["BuildSpec"]).is_file()


def test_pipeline_is_v2_queued_and_connects_all_artifacts():
    pipeline = resource("Pipeline")["Properties"]
    assert pipeline["PipelineType"] == "V2"
    assert pipeline["ExecutionMode"] == "QUEUED"
    assert pipeline["RestartExecutionOnUpdate"] is False
    assert pipeline["RoleArn"] == get_att("PipelineRole.Arn")
    assert pipeline["ArtifactStore"] == {
        "Type": "S3",
        "Location": ref("ArtifactBucket"),
    }
    # prodでは承認Stageが入り、dev / stgでは入りません。
    assert [stage["Name"] for stage in pipeline_stages()] == [
        "Source", "Test", "Build", "Approval", "Deploy"
    ]
    assert [stage["Name"] for stage in pipeline_stages(include_conditional=False)] == [
        "Source", "Test", "Build", "Deploy"
    ]
    actions_by_stage = {
        stage["Name"]: stage["Actions"][0] for stage in pipeline_stages()
    }
    source = actions_by_stage["Source"]
    assert source["ActionTypeId"] == {
        "Category": "Source",
        "Owner": "AWS",
        "Provider": "CodeStarSourceConnection",
        "Version": "1",
    }
    assert source["Configuration"] == {
        "ConnectionArn": ref("GitHubConnection"),
        "FullRepositoryId": ref("GitHubRepositoryId"),
        "BranchName": ref("BranchName"),
        "DetectChanges": "true",
        "OutputArtifactFormat": "CODE_ZIP",
    }
    assert source["OutputArtifacts"] == [{"Name": "SourceArtifact"}]

    expected = {
        "Test": (["SourceArtifact"], []),
        "Build": (["SourceArtifact"], ["BuildArtifact"]),
        "Deploy": (["SourceArtifact", "BuildArtifact"], []),
    }
    for phase, (inputs, outputs) in expected.items():
        action = actions_by_stage[phase]
        assert action["ActionTypeId"] == {
            "Category": "Build",
            "Owner": "AWS",
            "Provider": "CodeBuild",
            "Version": "1",
        }
        assert action["Configuration"] == {
            "ProjectName": ref(f"{phase}Project"),
            "PrimarySource": "SourceArtifact",
        }
        assert [item["Name"] for item in action["InputArtifacts"]] == inputs
        assert [item["Name"] for item in action.get("OutputArtifacts", [])] == outputs


def test_pipeline_role_can_only_use_declared_ci_resources():
    policy = statements("PipelineRole")
    assert policy["UseGitHubConnection"]["Resource"] == ref("GitHubConnection")
    assert set(policy["UseGitHubConnection"]["Action"]) == {
        "codeconnections:UseConnection",
        "codestar-connections:UseConnection",
    }
    assert policy["RunPipelineBuilds"]["Resource"] == [
        get_att("TestProject.Arn"),
        get_att("BuildProject.Arn"),
        get_att("DeployProject.Arn"),
    ]
    assert not any(item["Resource"] == "*" for item in policy.values())
    assert {action.split(":", 1)[0] for action in actions("PipelineRole")} == {
        "codeconnections", "codestar-connections", "codebuild", "s3", "sns"
    }
    # 通知先は自分のStackが作るTopicだけです。
    assert policy["NotifyApproval"]["Resource"] == ref("OperationsTopic")


def test_test_and_build_roles_cannot_update_aws_resources():
    forbidden_services = {
        "cloudformation", "iam", "lambda", "codedeploy", "scheduler", "sts"
    }
    for role_id in ("TestRole", "BuildRole"):
        role_actions = actions(role_id)
        assert {action.split(":", 1)[0] for action in role_actions} <= {"logs", "s3"}
        assert not any(action.endswith(":*") for action in role_actions)
        assert not forbidden_services & {
            action.split(":", 1)[0] for action in role_actions
        }
        assert not any(item["Resource"] == "*" for item in statements(role_id).values())


def test_deploy_role_is_limited_to_runtime_stacks_role_and_prefixes():
    policy = statements("DeployRole")
    assert {
        item["Sid"] for item in policy.values() if item["Resource"] == "*"
    } == {"VerifyDeploymentAccount"}
    assert policy["VerifyDeploymentAccount"]["Action"] == "sts:GetCallerIdentity"

    stack_arn = sub(
        "arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:"
        "stack/${ProjectName}-${Environment}-*/*"
    )
    for sid in ("CreateRuntimeChangeSets", "ManageRuntimeChangeSets", "ReadRuntimeStacks"):
        assert policy[sid]["Resource"] == stack_arn
    assert policy["CreateRuntimeChangeSets"]["Condition"] == {
        "ArnEquals": {"cloudformation:RoleArn": imported("RuntimeExecutionRoleArn")},
        "StringLike": {"cloudformation:ChangeSetName": "samcli-deploy*"},
    }
    # SAM CLIはDescribe / Execute / DeleteをChange SetのARNで呼ぶため、名前の条件を付けると拒否されます。
    assert "Condition" not in policy["ManageRuntimeChangeSets"]
    # 代わりに、Stack範囲に含まれるBootstrap / CI StackへのChange Set操作を拒否します。
    deny = policy["DenySetupStackChangeSets"]
    assert deny["Effect"] == "Deny"
    assert deny["Action"] == [
        "cloudformation:CreateChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:ExecuteChangeSet",
    ]
    assert deny["Resource"] == [
        sub(
            "arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:"
            f"stack/${{ProjectName}}-${{Environment}}-{name}/*"
        )
        for name in ("bootstrap", "ci")
    ]
    assert "cloudformation:GetTemplateSummary" in actions("DeployRole")
    assert policy["PassRuntimeExecutionRole"] == {
        "Sid": "PassRuntimeExecutionRole",
        "Effect": "Allow",
        "Action": "iam:PassRole",
        "Resource": imported("RuntimeExecutionRoleArn"),
        "Condition": {
            "StringEquals": {
                "iam:PassedToService": "cloudformation.amazonaws.com"
            }
        },
    }
    assert not ({"lambda", "codedeploy", "scheduler"} & {
        action.split(":", 1)[0] for action in actions("DeployRole")
    })


# 個人のメールアドレスをリポジトリへ残さないため、購読はTemplateで作りません。
def test_notifications_have_no_subscriber_in_the_template():
    assert not [
        name for name, item in template()["Resources"].items()
        if item["Type"] == "AWS::SNS::Subscription"
    ]
    assert "NotificationEmail" not in TEMPLATE_PATH.read_text(encoding="utf-8")


def test_topic_only_accepts_project_sources():
    rule = resource("PipelineFailureRule")["Properties"]
    assert rule["EventPattern"] == {
        "source": ["aws.codepipeline"],
        "detail-type": ["CodePipeline Pipeline Execution State Change"],
        "detail": {"pipeline": [ref("Pipeline")], "state": ["FAILED"]},
    }
    assert rule["Targets"] == [{"Id": "OperationsTopic", "Arn": ref("OperationsTopic")}]

    topic_statements = {
        item["Sid"]: item
        for item in resource("OperationsTopicPolicy")["Properties"]["PolicyDocument"]["Statement"]
    }
    assert set(topic_statements) == {"AllowProjectAlarms", "AllowPipelineFailureRule"}
    assert topic_statements["AllowProjectAlarms"]["Principal"] == {
        "Service": "cloudwatch.amazonaws.com"
    }
    assert topic_statements["AllowProjectAlarms"]["Condition"] == {
        "StringEquals": {"AWS:SourceAccount": ref("AWS::AccountId")},
        "ArnLike": {
            "AWS:SourceArn": sub(
                "arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:"
                "alarm:${ProjectName}-${Environment}-*"
            )
        },
    }
    assert topic_statements["AllowPipelineFailureRule"]["Principal"] == {
        "Service": "events.amazonaws.com"
    }
    assert topic_statements["AllowPipelineFailureRule"]["Condition"] == {
        "StringEquals": {
            "AWS:SourceAccount": ref("AWS::AccountId"),
            "AWS:SourceArn": get_att("PipelineFailureRule.Arn"),
        }
    }


# CodeBuildはSourceがCODEPIPELINEのとき、ArtifactsもCODEPIPELINEであることを要求します。
# cfn-lintとsam validateでは検出されず、Stack作成時に初めてCREATE_FAILEDになりました。
# 出力の有無はProjectではなくPipeline ActionのOutputArtifactsで表します。
def test_codepipeline_source_requires_codepipeline_artifacts():
    document = template()
    projects = {
        name: resource["Properties"]
        for name, resource in document["Resources"].items()
        if resource["Type"] == "AWS::CodeBuild::Project"
    }
    assert projects
    for name, properties in projects.items():
        if properties["Source"]["Type"] == "CODEPIPELINE":
            assert properties["Artifacts"]["Type"] == "CODEPIPELINE", name


# 出力を持たない段階は、ActionにOutputArtifactsを書かないことで表します。
def test_only_build_action_produces_an_output_artifact():
    outputs = {
        stage["Name"]: [artifact["Name"] for action in stage["Actions"]
                        for artifact in action.get("OutputArtifacts", [])]
        for stage in pipeline_stages()
    }
    assert outputs == {
        "Source": ["SourceArtifact"],
        "Test": [],
        "Build": ["BuildArtifact"],
        "Approval": [],
        "Deploy": [],
    }



# prodだけ、Buildの後・Deployの前で人の承認を待ちます。
# 承認Stageが無いまま配備されること、dev / stgで承認待ちになることの両方を防ぎます。
def test_production_requires_manual_approval_before_deploy():
    names = [stage["Name"] for stage in pipeline_stages()]
    assert names.index("Approval") == names.index("Deploy") - 1
    assert names.index("Build") < names.index("Approval")

    action = next(stage for stage in pipeline_stages()
                  if stage["Name"] == "Approval")["Actions"][0]
    assert action["ActionTypeId"] == {
        "Category": "Approval",
        "Owner": "AWS",
        "Provider": "Manual",
        "Version": "1",
    }
    # 承認待ちは既存の通知Topicへ送ります。承認画面には対象の環境を表示します。
    assert action["Configuration"]["NotificationArn"] == ref("OperationsTopic")
    assert "${ProjectName}-${Environment}" in action["Configuration"]["CustomData"]["!Sub"]
    assert "InputArtifacts" not in action
