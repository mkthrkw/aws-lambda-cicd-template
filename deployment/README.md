# Deployment

PipelineのTest / Build / Deployで動く処理を置きます。**AWS標準機能で足りない部分だけ**を、3つのPythonモジュールに閉じ込めています。

| パス | 内容 |
| --- | --- |
| `buildspecs/test.yml` | Test Stage。pytestを実行する |
| `buildspecs/build.yml` | Build Stage。Parameterを検証し、全Runtimeを`sam build`する |
| `buildspecs/deploy.yml` | Deploy Stage。全Runtimeへ`sam deploy`する |
| `scripts/parameters.py` | 設定値の読み込み・検証・4層の解決 |
| `scripts/build.py` | 全Runtimeのビルドと、共有コードの同梱 |
| `scripts/deploy.py` | 解決した設定値を渡して`sam deploy`を実行 |
| `tests/` | 上記3モジュールのUnit Test（AWS CLIとSAMは模擬化） |

自作しているのはここだけです。Version発行、Alias切替、Rollback、デプロイ対象の選別は持ちません。

## 手元で実行する

リポジトリルートからPython moduleとして実行します。

```bash
# 設定値の検証だけ（AWSへ接続しない）
python -m deployment.scripts.parameters --environment dev

# PipelineのBuild Stageと同じ処理（build/ に出力。SAM CLIが必要）
python -m pip install -r deployment/buildspecs/requirements.txt
python -m deployment.scripts.build --environment dev --region ap-northeast-1
```

3つ目のDeployコマンドは**実際にAWSのResourceを更新します**。通常の開発では使いません（PipelineのDeploy Stageが実行します）。

## 各処理の内容

### Test

`pytest`を実行します。ここで失敗すると以降のStageは動かず、**AWSは何も変わりません**。

テストは`application/tests`、`infrastructure/tests`、`deployment/tests`の3つを対象にします。Templateの構造やStack間の受け渡しも、この段階で検証されます。

### Build

処理の順序は次のとおりです。

1. 全Runtimeの設定値を解決し、検証する（1つでも不正なら中断）
2. 全Templateを`sam validate --lint`で確認する
3. Runtimeごとに`sam build`を実行する
4. 共有コード（`application/common/`）を、ビルドされた各関数のディレクトリへコピーする
5. 設定値をBuildArtifactへ同梱する

出力先は`build/`に固定し、開始時に中身を削除します。`build`という名前の通常ファイルやシンボリックリンクがある場合は、削除せずエラーにします。

失敗した場合は`build/`を削除し、SAMの終了コードをそのまま返します。中途半端な成果物が次のStageへ渡ることはありません。

### BuildArtifactの構造

```text
build/                         # 展開後はBuildArtifactのルート
├ runtimes/
│  ├ sample-batch-a/
│  │  ├ template.yaml          # sam buildの生成物
│  │  └ Function/              # 生成TemplateのCodeUri参照先。共有コードを含む
│  └ sample-flow-a/            # 複数の関数を持つRuntimeは関数の論理IDごとに並ぶ
│     ├ template.yaml
│     ├ Fetch/
│     ├ Transform/
│     └ Notify/
└ configuration/
   ├ runtime-parameters.json    # 対象の1環境分だけ
   └ runtimes/
      ├ sample-batch-a.json
      └ sample-flow-a.json
```

関数のディレクトリ名は、Template内の論理IDです（`Function`、`Fetch`など）。生成Templateの`CodeUri`がこの名前を指します。

Templateだけでなく、Templateが参照するコードと依存ライブラリを一緒に保存します。別のCodeBuild作業ディレクトリへ展開しても`CodeUri`を解決できるよう、ビルド環境固有の絶対パスを残しません。

### Deploy

BuildArtifactのTemplateと設定値だけを使って`sam deploy`を実行します。**ここでソースを読み直したり再ビルドしたりしません。**

Runtime名の辞書順に、1つずつ配備します。

```bash
sam deploy \
  --template-file "$BUILT_TEMPLATE" \
  --stack-name "$STACK_NAME" \
  --s3-bucket "$DEPLOYMENT_BUCKET" \
  --s3-prefix "runtimes/$STACK_NAME" \
  --role-arn "$CFN_EXECUTION_ROLE_ARN" \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags Project="$PROJECT_NAME" Environment="$DEPLOY_ENV" ManagedBy=CloudFormation \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides <解決した全Parameter>
```

| 指定 | 理由 |
| --- | --- |
| `--role-arn` | Resourceを作るのはCloudFormationのExecution Role。CodeBuildのRoleには作る権限を持たせない |
| `--capabilities CAPABILITY_NAMED_IAM` | Runtime TemplateがIAM Roleに名前を付けるため |
| `--tags` | Stackにタグを付け、CloudFormationに各Resourceへ伝播させる |
| `--no-fail-on-empty-changeset` | 毎回全Stackを処理するため、変更のないStackが必ず出る |
| `--parameter-overrides` | 前回値に依存せず、解決した最終値を毎回すべて明示的に渡す |

`--no-fail-on-empty-changeset`は「変更が無いことを成功とする」指定で、デプロイの失敗を無視するものではありません。

実行前に、配備先のAccountが想定どおりかを`sts get-caller-identity`で確認します。1つのRuntimeで失敗したら後続を止め、非ゼロで終了します。**すでに成功したStackを自動では戻しません。**

## Deployに必要な環境変数

CodeBuildではCI Stackが渡します。手元で実行する場合はすべて自分で設定します。

| 環境変数 | 設定内容 |
| --- | --- |
| `PROJECT_NAME` | 小文字英数字とハイフンによるProject名。20文字まで |
| `DEPLOY_ENV` | `dev` / `stg` / `prod`。Artifact内の`Environment`と一致させる |
| `AWS_REGION` | 配備先Region |
| `EXPECTED_ACCOUNT_ID` | 配備を許可した12桁のAccount ID |
| `DEPLOYMENT_BUCKET` | SAM資材を置くBucket名（S3 URIではない） |
| `CFN_EXECUTION_ROLE_ARN` | CloudFormation Execution RoleのARN |
| `OPERATIONS_TOPIC_ARN` | 通知用SNS TopicのARN |

`EXPECTED_ACCOUNT_ID`は誤ったAccountへの配備を防ぐためのものです。STSの結果と一致しなければ、Resourceを触る前に停止します。

`AlarmTopicArn`と`ProjectName`は、JSONに書かずここから実行時に注入されます。Account IDを含む値をGitへ残さないためです。

## 設定値の4層

`scripts/parameters.py`が、次の順で値を重ねます（あとの層が優先）。

```text
4  env/<環境>/runtime-parameters.json の runtimes.<Runtime名>   その環境のそのRuntimeだけ
3  env/<環境>/runtime-parameters.json の parameters             その環境の共通値
2  runtimes/<Runtime名>/parameters.json                         そのRuntimeの全環境共通
1  Template の Parameters.<名前>.Default                        既定値
```

環境共通値（3）は、**そのParameterを宣言しているRuntimeにだけ適用します**。定期実行を持つRuntimeと持たないRuntimeが混在できるようにするためです。どのRuntimeも宣言していないParameterを書いた場合は、書き間違いとしてエラーにします。

書き方の規則は[infrastructure/env/README.md](../infrastructure/env/README.md)にあります。

## テスト

```bash
python -m pytest deployment/tests
```

AWS CLIとSAMの呼び出しを模擬化しているため、AWSへは接続しません。確認しているのは主に次の点です。

| 確認内容 |
| --- |
| 解決した設定値を毎回すべて`sam deploy`へ渡すこと |
| Artifactの中身を1バイトも書き換えないこと |
| 共通のタグをStackへ渡すこと |
| 1つ失敗したら後続を実行しないこと |
| エラーに設定値そのものを出力しないこと |
