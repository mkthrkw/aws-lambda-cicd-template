# lambda-simple-template

定期実行するAWS Lambdaのために、**CI/CDと実行基盤を最小構成でそろえたテンプレート**です。複製してサンプルを業務処理へ置き換えると、GitHubへpushするだけでLambdaが更新される状態から開発を始められます。

仕組みはAWSの標準機能へ寄せています。デプロイ対象の選別、Version発行、Alias切替、Rollbackといった処理を自作せず、CloudFormation・SAM・CodePipeline・CodeDeployに任せています。**自作コードが少ないほど、変更箇所を予測しやすく障害原因を追いやすい**という考え方です。

```text
開発者 ── push ──> GitHub ──> CodePipeline ──> CloudFormation ──> Lambda
                                Source              （定期実行、ログ、監視つき）
                                Test    pytest
                                Build   sam build
                                Deploy  sam deploy
```

## 特徴

- **1つのStackに、まとめて配備したい範囲を入れる。** 関数、実行Role、Log Group、Alarm、定期実行を1つのTemplateで追えます
- **毎回すべてのStackを配備する。** Git差分による対象の選別を持ちません。変更の有無はCloudFormationが判断します
- **`live` Aliasだけを呼ぶ。** 呼び出し先を固定し、切替をCodeDeployに任せます
- **Pipelineに強い権限を渡さない。** Resourceを作るのはCloudFormationのExecution Roleだけです
- **秘密情報をGitに置かない。** Account ID、ARN、通知の宛先はリポジトリに含めません
- **AWSなしでテストできる。** Python 3.12だけでUnit Testが動きます

## 収録しているサンプル

3種類のRuntime（デプロイ単位）を収録しています。いずれも外部サービスへ接続せず、仕組みが動くことを確認するための最小限の処理です。

| Runtime | 起動のきっかけ | 構成 |
| --- | --- | --- |
| `sample-batch-a` | 時刻（EventBridge Scheduler） | Lambda 1つ |
| `sample-flow-a` | 時刻 → Step Functions | Lambda 3つを順に実行するジョブ |
| `sample-queue-a` | メッセージ（SQS） | Queue、デッドレターキュー、消費するLambda |

## 構成

CloudFormationのStackを、変更の頻度で3つに分けています。

| Stack | 中身 | 誰が更新するか |
| --- | --- | --- |
| Bootstrap | CloudFormation Execution Role、SAM資材のS3 Bucket | 管理者（環境ごとに1回） |
| CI | CodePipeline、CodeBuild、GitHub接続、通知用SNS Topic | 管理者 |
| Runtime | Lambda、実行Role、Log Group、Alarm、Scheduler等 | **Pipeline** |

Pipelineが触るのはRuntime Stackだけです。Pipeline自身の定義（CI Stack）と権限の定義（Bootstrap Stack）はPipelineから更新しません。Pipelineが壊れたときにPipelineで直す状況を避けるためです。

環境（dev / stg / prod）は**別のAWS Accountに分ける**前提です。環境ごとにBootstrap / CI Stackを1組ずつ作ります。

## ディレクトリ構成

```text
.
├ application/                    # Lambdaの実行コードとテスト
│  ├ common/                      # 全Lambdaへ同梱する共有コード
│  ├ functions/<関数名>/          # handler.py と requirements.txt
│  └ tests/                       # pytestのテスト
├ infrastructure/                 # Template（設計）と設定値
│  ├ bootstrap/template.yaml
│  ├ ci/template.yaml
│  ├ runtimes/<Runtime名>/        # Runtime 1つ分。template.yaml と parameters.json
│  ├ env/<環境>/                  # 環境ごとの設定値
│  └ tests/                       # Templateの構造テスト
├ deployment/                     # Test / Build / Deployの処理
│  ├ buildspecs/                  # CodeBuildの定義
│  ├ scripts/                     # Parameter解決、Build、Deploy
│  └ tests/
├ pytest.ini
└ requirements-dev.txt
```

`infrastructure/runtimes/`のディレクトリ名がRuntime名になり、Stack名は`<Project名>-<環境>-<Runtime名>`です。**別途Runtime一覧を管理するファイルは持ちません。** ディレクトリを置くことが、そのままデプロイ対象になります。

より詳しい説明は、それぞれのREADMEにあります。

| README | 内容 |
| --- | --- |
| [infrastructure/README.md](infrastructure/README.md) | Templateの設計。Runtime Stackの中身、Version / Alias、監視、IAMの考え方 |
| [infrastructure/env/README.md](infrastructure/env/README.md) | 設定値の書き方。2つのJSON形式と、4層の優先順位 |
| [deployment/README.md](deployment/README.md) | Test / Build / Deployの処理と、BuildArtifactの構造 |

## 手元で動かす

必要なのはPython 3.12だけです。AWS Account、SAM CLI、DockerはUnit Testに要りません。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

設定値が正しく組み立てられるかも、AWSへ接続せずに確認できます。

```bash
python -m deployment.scripts.parameters --environment dev
```

```text
sample-batch-a: parameters valid (7)
sample-flow-a: parameters valid (7)
sample-queue-a: parameters valid (6)
```

Templateの確認とビルドには、SAM CLIが必要です。

```bash
sam validate --lint --region ap-northeast-1 \
  --template-file infrastructure/runtimes/sample-batch-a/template.yaml

# PipelineのBuild Stageと同じ処理（build/ に出力）
python -m pip install -r deployment/buildspecs/requirements.txt
python -m deployment.scripts.build --environment dev --region ap-northeast-1
```

## AWSへ構築する

環境ごとに1回だけ行います。作業はAWS CloudShellから実行できるため、開発端末にAWS CLIの設定は要りません。

### 1. 設定値を書き換える

`infrastructure/env/<環境>/setup-parameters.json`を、自分のプロジェクトの値に変更します。

```json
[
  { "ParameterKey": "ProjectName",        "ParameterValue": "example-project" },
  { "ParameterKey": "Environment",        "ParameterValue": "dev" },
  { "ParameterKey": "GitHubRepositoryId", "ParameterValue": "example-owner/example-repository" },
  { "ParameterKey": "BranchName",         "ParameterValue": "develop" }
]
```

`ProjectName`は小文字英数字とハイフンで20文字までです。Resource名の先頭に付きます。

### 2. Bootstrap Stackを作る

CloudShellでリポジトリを展開し、次を実行します。

```bash
export PROJECT_NAME="example-project"
export ENVIRONMENT="dev"
export SETUP_PARAMETER_FILE="infrastructure/env/${ENVIRONMENT}/setup-parameters.json"

aws cloudformation deploy \
  --template-file infrastructure/bootstrap/template.yaml \
  --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-bootstrap" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "file://${SETUP_PARAMETER_FILE}" \
  --tags Project="$PROJECT_NAME" Environment="$ENVIRONMENT" ManagedBy=CloudFormation \
  --no-fail-on-empty-changeset \
  --no-cli-pager
```

### 3. CI Stackを作る

```bash
aws cloudformation deploy \
  --template-file infrastructure/ci/template.yaml \
  --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-ci" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "file://${SETUP_PARAMETER_FILE}" \
  --tags Project="$PROJECT_NAME" Environment="$ENVIRONMENT" ManagedBy=CloudFormation \
  --no-fail-on-empty-changeset \
  --no-cli-pager
```

Execution RoleのARNとBucket名を渡す必要はありません。CI StackがBootstrap StackのExportから取り込みます。

`--tags`はStackにタグを付けます。CloudFormationが各Resourceへ伝播させるため、**Templateにタグを書きません**。省略して更新すると既存のタグが外れます。

### 4. GitHub接続を認証する

CloudFormationで作成したGitHub接続（CodeConnections）は、**作成直後は`PENDING`（未認証）**です。ここだけコンソールで操作します。

1. CodePipelineのコンソールで「設定」→「接続」を開く
2. 作成された接続を選び「保留中の接続を更新」
3. GitHubの認証画面で、対象リポジトリへのアクセスを許可する
4. 状態が`AVAILABLE`になることを確認する

GitHubのTokenをParameterで渡さないのは、秘密情報をGitやCloudFormationに置かないためです。

### 5. 通知の受け取り先を登録する

Lambdaの異常とPipelineの失敗は、環境ごとに1つのSNS Topic（`<Project名>-<環境>-operations`）へ集約されます。**購読先はTemplateで管理していません**（個人のメールアドレスをリポジトリに残さないため）。

```bash
OPERATIONS_TOPIC_ARN="$(
  aws cloudformation describe-stacks \
    --stack-name "${PROJECT_NAME}-${ENVIRONMENT}-ci" \
    --query "Stacks[0].Outputs[?OutputKey=='OperationsTopicArn'].OutputValue | [0]" \
    --output text --no-cli-pager
)"

aws sns subscribe \
  --topic-arn "$OPERATIONS_TOPIC_ARN" \
  --protocol email \
  --notification-endpoint "宛先のメールアドレス" \
  --no-cli-pager
```

確認メールのリンクを開くまで通知は届きません。登録後に一度、意図的に失敗させて到達を確認しておくと安全です。

### 6. pushする

対象branchへpushすると、Pipelineが動いてRuntime Stackが作られます。これ以降、Lambdaの更新はpushだけで完了します。

## Pipelineの流れ

| Stage | 処理 | 失敗したとき |
| --- | --- | --- |
| Source | GitHubの対象branchを取得 | 以降は動かない |
| Test | `pytest` | **AWSは何も変わらない** |
| Build | 全Runtimeを`sam build` | AWSは何も変わらない |
| Approval | prodのみ。人が承認する | 既定7日で失効し失敗 |
| Deploy | 全Runtimeへ`sam deploy` | 途中まで配備された状態になり得る |

prod環境だけ、BuildとDeployの間に手動承認が入ります。

```text
dev / stg : Source → Test → Build ─────────────→ Deploy
prod      : Source → Test → Build → Approval → Deploy
```

承認する担当者には`codepipeline:PutApprovalResult`が必要です。この権限はTemplateで付与していないため、IAMで別途設定します。

Pipelineは`ExecutionMode: QUEUED`で動きます。連続してpushしても後の実行が先の実行を追い越さないため、同じStackが同時に更新されることはありません。

## Lambdaを追加する

```bash
# 1. 関数のコードを置く（ディレクトリ名はPythonのモジュール名になるためアンダースコア）
cp -r application/functions/sample_batch_a application/functions/daily_report

# 2. テストを追加する
cp application/tests/test_sample_batch_a.py application/tests/test_daily_report.py

# 3. Runtimeのディレクトリをコピーする（ディレクトリ名がRuntime名。20文字まで）
cp -r infrastructure/runtimes/sample-batch-a infrastructure/runtimes/daily-report
```

`infrastructure/runtimes/daily-report/template.yaml`で変更するのは`CodeUri`の1行だけです。

```yaml
      CodeUri: ../../../application/functions/daily_report/
```

論理IDは`Function`や`ExecutionRole`のような汎用名で、Resource名はStack名から付きます。実行間隔は`parameters.json`で指定します。

```json
{
  "ScheduleExpression": "cron(0 9 * * ? *)"
}
```

手元で確認してpushすれば、Pipelineが新しいStackを作ります。

```bash
python -m pytest
python -m deployment.scripts.parameters --environment dev
```

ジョブ（複数の関数）やキュー処理を作る場合は、`sample-flow-a`か`sample-queue-a`をコピーします。違いは[infrastructure/README.md](infrastructure/README.md)にあります。

## 向いている範囲

| 観点 | この構成が合う状況 |
| --- | --- |
| Lambdaの数 | 初期は数個、最大で10〜15個程度。毎回全Stackを処理しても待てる |
| 処理の種類 | 定期実行のバッチ処理。キューを受け口にした非同期処理 |
| リリース | `live` Aliasへの一括切替で足りる |
| 運用体制 | 少人数で、独自の仕組みを増やしたくない |

次のような要求が出てきた場合は、この構成のままでは満たせません。

| 必要になること | 追加で設計する内容 |
| --- | --- |
| Lambda数が増えて全件ビルドが重い | 変更検出による絞り込み、キャッシュ、並列実行 |
| 段階的リリースと自動判定 | Canary / Linearでの切替、判定メトリクスとRollback条件 |
| 同じ成果物を環境間で昇格させる | Artifactの受け渡しと昇格の記録 |
| API GatewayやS3イベントからの起動 | 起動元Resourceと権限、再試行と重複実行の設計 |
| 状態を持つResource（VPC、RDS等）の同居 | Stackの分割、データ移行、Rollback不能な変更の扱い |

## つまずいたときは

Deployで権限エラーが出た場合、エラーの読み方は決まっています。

```text
User: arn:aws:sts::123456789012:assumed-role/<Role名>/<セッション名>
is not authorized to perform: <Action> on resource: <Resource ARN>
```

| 見る場所 | 分かること |
| --- | --- |
| `assumed-role/`の直後 | どのRoleが足りないか。`-ci-DeployRole-`ならCI Stack、`-bootstrap-ExecutionRole-`ならBootstrap Stack |
| `to perform:` | 追加すべきAction |
| `on resource:` | 許可のResourceに一致しているか |

**権限の変更はPipelineでは反映されません。** 管理者が手順2または3のコマンドで、該当のStackを更新します。

よく出るものを挙げます。

| 症状 | 意味 | 対処 |
| --- | --- | --- |
| `is in ROLLBACK_COMPLETE state and can not be updated` | 初回作成に失敗し、空の器だけが残っている | Stackを削除してから作り直す |
| `UPDATE_ROLLBACK_COMPLETE`で終わる | 更新に失敗して前の状態へ戻った | 原因を直して再実行（削除は不要） |
| `A version for this Lambda function exists ( N )` | コードも設定も変わっていないため、LambdaがVersionを発行できない | コードか設定（`Description`等）の変更を同じデプロイに含める |
| `UnauthorizedTaggingOperation` | Stackタグを伝播する先のResourceにTag権限が足りない | エラー本文のActionとARNをExecution Roleへ追加する |
| `No Branch [xxx] found` | `BranchName`が存在しないbranchを指している | branch名を直してCI Stackを更新する |
| Source Actionが失敗し、接続の問題と表示される | GitHub接続が`PENDING`のまま | 構築手順4の認証を行う |
| `No changes to deploy`で成功する | 変更が無い状態。異常ではない | そのままで問題ない。Versionも増えない |

## 前提

| 項目 | 値 |
| --- | --- |
| Lambda Runtime | Python 3.12 / x86_64 |
| IaC | CloudFormation、AWS SAM |
| CI/CD | CodePipeline（V2）、CodeBuild、CodeConnections、CodeDeploy |
| 開発 | Python 3.12、pytest。SAM CLIは任意 |
| ソース管理 | GitHub |
