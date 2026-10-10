# Bootstrap Stack TemplateのIAM境界を、AWSへ接続せずに確認します。
# lintだけではPolicyの対象範囲やPassRole条件までは検証できないため、
# Runtime Stack以外を操作できる変更が混入していないことを構造として固定します。

from pathlib import Path

from deployment.scripts.parameters import load_template


ROOT = Path(__file__).resolve().parents[2]  # リポジトリルート
TEMPLATE_PATH = ROOT / "infrastructure/bootstrap/template.yaml"


def template():
    return load_template(TEMPLATE_PATH)


def statements():
    role = template()["Resources"]["ExecutionRole"]
    return {
        item["Sid"]: item
        for item in role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    }


def sub_value(value):
    return value["!Sub"]


def test_parameters_and_output_are_the_documented_interface():
    document = template()
    assert document["Parameters"] == {
        "ProjectName": {
            "Type": "String",
            "Description": "Lowercase project prefix shared by Bootstrap, CI, and Runtime stacks.",
            "MinLength": 1,
            "MaxLength": 20,
            "AllowedPattern": "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
            "ConstraintDescription": "Use lowercase letters, numbers, and single hyphens; start with a letter.",
        },
        "Environment": {
            "Type": "String",
            "Description": "Deployment environment managed in this AWS account.",
            "AllowedValues": ["dev", "stg", "prod"],
        },
    }
    assert document["Outputs"]["RuntimeExecutionRoleArn"]["Value"] == {
        "!GetAtt": "ExecutionRole.Arn"
    }
    # CI StackがImportValueで取り込む2つの値です。
    assert set(document["Outputs"]) == {"RuntimeExecutionRoleArn", "DeploymentBucketName"}
    assert document["Outputs"]["DeploymentBucketName"]["Value"] == {"!Ref": "DeployBucket"}
    assert document["Outputs"]["DeploymentBucketName"]["Export"]["Name"] == {
        "!Sub": "${ProjectName}-${Environment}-bootstrap-DeploymentBucketName"
    }


def test_bootstrap_contains_the_execution_role_and_deployment_bucket():
    resources = template()["Resources"]
    assert {name: item["Type"] for name, item in resources.items()} == {
        "DeployBucket": "AWS::S3::Bucket",
        "DeployBucketPolicy": "AWS::S3::BucketPolicy",
        "ExecutionRole": "AWS::IAM::Role",
    }
    role = resources["ExecutionRole"]
    assert role["Type"] == "AWS::IAM::Role"
    assert "RoleName" not in role["Properties"]
    # Runtime Stackが操作できる /runtime/ とは別のPathに置き、自分自身の権限を変更させません。
    assert role["Properties"]["Path"] == {"!Sub": "/${ProjectName}/${Environment}/cloudformation/"}
    assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Sid": "CloudFormationOnly",
            "Effect": "Allow",
            "Principal": {"Service": "cloudformation.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ]


# 影響の大きいLambdaとIAMは、使うActionだけを列挙します。Resourceを限定していても、
# 関数の公開設定やRole・Policyの変更まで一括で許可しないためです。
# Logs、CloudWatch、CodeDeploy、Step Functions、SchedulerはService単位で許可し、
# 範囲はResourceの限定で決めます（設定を1つ足すたびのBootstrap更新を避けるため）。
SERVICES_ALLOWED_AS_A_WHOLE = {"logs", "cloudwatch", "codedeploy", "scheduler", "states"}


def test_every_runtime_service_is_covered_and_only_low_risk_ones_use_wildcards():
    policy_statements = statements()
    actions = {
        action
        for item in policy_statements.values()
        for action in (item["Action"] if isinstance(item["Action"], list) else [item["Action"]])
    }
    assert {action.split(":", 1)[0] for action in actions} == {
        "cloudformation", "lambda", "iam", "logs", "cloudwatch", "codedeploy",
        "scheduler", "states", "sqs", "s3",
    }
    assert {action.split(":", 1)[0] for action in actions if action.endswith(":*")} == (
        SERVICES_ALLOWED_AS_A_WHOLE
    )
    assert {
        "lambda:CreateFunction",
        "lambda:PublishVersion",
        "lambda:CreateAlias",
        "lambda:CreateEventSourceMapping",
        "iam:CreateRole",
        "sqs:CreateQueue",
        # Resourceを限定できないため、Service単位の許可に含まれないAction
        "logs:DescribeLogGroups",
        "codedeploy:CreateCloudFormationDeployment",
        "cloudformation:CreateChangeSet",
    } <= actions
    # SQSはService単位にしません。メッセージそのものを操作する権限が入るためです。
    assert not {"sqs:SendMessage", "sqs:ReceiveMessage", "sqs:PurgeQueue"} & actions


# Service単位で許可するのは、対象Resourceを限定できるStatementだけです。
# Resourceが '*' のStatementへ広げると、他のStackのResourceまで操作できてしまいます。
def test_service_wide_statements_are_limited_to_project_resources():
    for sid, item in statements().items():
        action = item["Action"]
        if not (isinstance(action, str) and action.endswith(":*")):
            continue
        resources = item["Resource"] if isinstance(item["Resource"], list) else [item["Resource"]]
        for resource in resources:
            assert "${ProjectName}-${Environment}-" in sub_value(resource), (sid, resource)


def test_scoped_statements_use_only_project_environment_resources():
    policy_statements = statements()
    wildcard_sids = {
        sid for sid, item in policy_statements.items() if item["Resource"] == "*"
    }
    assert wildcard_sids == {
        "ReadLogGroupInventory",
        "RunCloudFormationCodeDeployUpdate",
        "ManageRuntimeEventSourceMappings",
    }
    assert policy_statements["ReadLogGroupInventory"]["Action"] == "logs:DescribeLogGroups"
    assert policy_statements["RunCloudFormationCodeDeployUpdate"]["Action"] == (
        "codedeploy:CreateCloudFormationDeployment"
    )
    # Event Source MappingのARNには乱数のIDが入り、ARNでは範囲を決められません。
    # 代わりに接続先の関数をConditionで限定します。
    mapping = policy_statements["ManageRuntimeEventSourceMappings"]
    assert all(action.startswith("lambda:") and action.endswith("EventSourceMapping")
               for action in mapping["Action"])
    assert sub_value(mapping["Condition"]["StringLike"]["lambda:FunctionArn"]).endswith(
        ":function:${ProjectName}-${Environment}-*"
    )

    # Event Source MappingのTag操作は、AWSが event-source-mapping:* に対して
    # 権限を確認するため、ProjectとEnvironmentで範囲を絞れません（実環境で確認）。
    # Tagの読み書き以外は含めず、対象の種類をEvent Source Mappingに限ります。
    tagging = policy_statements["TagRuntimeEventSourceMappings"]
    assert set(tagging["Action"]) == {"lambda:ListTags", "lambda:TagResource", "lambda:UntagResource"}
    assert sub_value(tagging["Resource"]).endswith(":event-source-mapping:*")

    for sid, item in policy_statements.items():
        if sid in wildcard_sids or sid in (
            "UseServerlessTransform", "ReadLambdaDeploymentConfiguration", "ReadRuntimeArtifacts",
            "TagRuntimeEventSourceMappings",
        ):
            continue
        resources = item["Resource"] if isinstance(item["Resource"], list) else [item["Resource"]]
        for resource in resources:
            arn = sub_value(resource)
            assert "${AWS::Partition}" in arn
            assert "${AWS::AccountId}" in arn
            if sid in ("ManageRuntimeRoles", "PassRuntimeRolesToServices"):
                # IAM Roleだけは名前ではなくPathで範囲を決めます。
                assert arn.endswith(":role/${ProjectName}/${Environment}/runtime/*")
            else:
                assert "${ProjectName}-${Environment}-*" in arn

    deployment_config = sub_value(
        policy_statements["ReadLambdaDeploymentConfiguration"]["Resource"]
    )
    assert deployment_config.endswith(
        ":deploymentconfig:CodeDeployDefault.LambdaAllAtOnce"
    )


# CloudFormation Execution RoleでSAM Transformを処理するときに必要な権限です。
# 実AccountではなくAWS管理のTransform ARNだけへ限定します。
def test_execution_role_can_use_only_the_serverless_transform():
    assert statements()["UseServerlessTransform"] == {
        "Sid": "UseServerlessTransform",
        "Effect": "Allow",
        "Action": "cloudformation:CreateChangeSet",
        "Resource": {
            "!Sub": (
                "arn:${AWS::Partition}:cloudformation:${AWS::Region}:aws:"
                "transform/Serverless-2016-10-31"
            )
        },
    }


# 名前付きRoleとInline Policyの作成前確認は、Pathを含まないARNで判定されます。
# その確認のための許可は読み取りだけに限り、変更系の操作は含めません。
def test_role_name_check_before_creation_is_read_only():
    statement = statements()["CheckRuntimeRoleNames"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["iam:GetRole", "iam:GetRolePolicy"]
    assert sub_value(statement["Resource"]).endswith(":role/${ProjectName}-${Environment}-*")
    assert "Condition" not in statement


def test_pass_role_is_limited_to_runtime_roles_and_three_services():
    statement = statements()["PassRuntimeRolesToServices"]
    assert statement["Action"] == "iam:PassRole"
    assert sub_value(statement["Resource"]).endswith(
        ":role/${ProjectName}/${Environment}/runtime/*"
    )
    assert statement["Condition"] == {
        "StringEquals": {
            "iam:PassedToService": [
                "lambda.amazonaws.com",
                "scheduler.amazonaws.com",
                "codedeploy.amazonaws.com",
                "states.amazonaws.com",
            ]
        }
    }


# LambdaはCloudFormationが引き受けるExecution Roleの権限で、コードのZIPをS3から取得します。
# 対象は同じStackのDeployment Bucketの runtimes/ だけで、書き込みは許可しません。
def test_execution_role_reads_only_runtime_artifacts_in_the_same_stack():
    statement = statements()["ReadRuntimeArtifacts"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["s3:GetObject", "s3:GetObjectVersion"]
    assert statement["Resource"] == {"!Sub": "${DeployBucket.Arn}/runtimes/*"}


# SAM資材の置き場所です。名前は固定せず、非公開・暗号化・Versioning・保持を設定します。
def test_deployment_bucket_is_private_encrypted_versioned_and_retained():
    resources = template()["Resources"]
    bucket = resources["DeployBucket"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    properties = bucket["Properties"]
    assert "BucketName" not in properties
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
    assert rules["ExpireRuntimeArtifacts"] == {
        "Id": "ExpireRuntimeArtifacts",
        "Status": "Enabled",
        "Prefix": "runtimes/",
        "ExpirationInDays": 180,
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    }
    assert rules["AbortIncompleteUploads"]["AbortIncompleteMultipartUpload"] == {
        "DaysAfterInitiation": 7
    }
    # TLS以外の通信だけを拒否します。読み取りの許可はRole側に書くため、Bucket Policyに置きません。
    assert resources["DeployBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"] == [{
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [
            {"!GetAtt": "DeployBucket.Arn"},
            {"!Sub": "${DeployBucket.Arn}/*"},
        ],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }]
