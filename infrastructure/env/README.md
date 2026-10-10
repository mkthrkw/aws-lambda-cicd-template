# 環境ごとの設定ファイル

環境（`dev` / `stg` / `prod`）ごとに、2つのJSONファイルを置きます。名前も形式も役割も違うため、書き始める前にこの文書を確認してください。

| ファイル | 誰が読むか | 形式 | いつ効くか |
| --- | --- | --- | --- |
| `setup-parameters.json` | `aws cloudformation deploy` | 配列 | 管理者がBootstrap / CI Stackを作成・更新するとき |
| `runtime-parameters.json` | `deployment/scripts/parameters.py` | マップ | PipelineがLambdaを配備するとき |

形式が違うのは、読む主体が違うためです。`setup-parameters.json`はAWS CLIがCloudFormationへ直接渡すため、CLIが決めた配列形式に従います。`runtime-parameters.json`は`deployment/scripts/parameters.py`が読み、4層を重ねて最終値を決めてから`sam deploy`へ渡すため、扱いやすいマップ形式にしています。

## 共通の書き方

- 文字コードはUTF-8です。
- **値はすべて文字列**です。数値も`"14"`のように引用符で囲みます。`14`と書くとエラーになります。
- 真偽値、`null`、配列、オブジェクトは値に使えません。
- 空文字`""`は「未設定」ではなく、有効な明示値として扱います。
- JSONにコメントは書けません。補足が必要なときはこの文書を更新してください。
- 同じキーを2回書けません。読み込み時にエラーになります。
- Account ID、ARN、トークン、秘密鍵は書きません。必要な値はAWSから実行時に取得します。

## setup-parameters.json

Bootstrap StackとCI Stackへ渡す値です。CloudFormationのParameterファイル形式で書き、`--parameter-overrides file://`でそのまま渡します。

名前の`setup`は、管理者が構築に使うことを表します。初回の構築だけでなく、Bootstrap / CI Stackを更新するときも同じファイルを使います（[構築手順](../../README.md#awsへ構築する)）。

Pipelineはこのファイルを配備に使いません。Test Stageの契約テストが、キーと形式を検証するために読むだけです。Bootstrap StackとCI StackはPipelineから更新しない設計のためです。

```json
[
  {
    "ParameterKey": "ProjectName",
    "ParameterValue": "example-project"
  },
  {
    "ParameterKey": "Environment",
    "ParameterValue": "dev"
  },
  {
    "ParameterKey": "GitHubRepositoryId",
    "ParameterValue": "example-owner/example-repository"
  },
  {
    "ParameterKey": "BranchName",
    "ParameterValue": "develop"
  }
]
```

| キー | 書く内容 | 間違えやすい点 |
| --- | --- | --- |
| `ProjectName` | Stack名の接頭辞 | 小文字英数字と単一ハイフンのみ。最大20文字 |
| `Environment` | `dev` / `stg` / `prod` | 置き場のディレクトリ名と必ず一致させる |
| `GitHubRepositoryId` | `owner/repository` | URLではなくこの形式。`https://`は付けない |
| `BranchName` | 監視するbranch | 実在しない名前だとPipelineのSource Stageが失敗する |

4項目すべてが必要です。1つでも欠けると契約テストが失敗します。通知の宛先はここに書きません。構築後にSNS Topicへ登録します（[構築手順](../../README.md#5-通知の受け取り先を登録する)）。

Execution RoleのARNはここに書きません。CI StackがBootstrap StackのExportから取り込みます。Account IDも書きません。CI Templateが`AWS::AccountId`から取得します。

## runtime-parameters.json

Lambdaを動かすRuntime Stackへ渡す、その環境の設定です。`parameters`と`runtimes`の2つのキーだけを持ちます。どちらも必須です。

```json
{
  "parameters": {
    "Environment": "dev",
    "ScheduleState": "DISABLED"
  },
  "runtimes": {}
}
```

| キー | 書く内容 |
| --- | --- |
| `parameters` | その環境の**全Runtime**へ適用する値 |
| `runtimes` | Runtime名をキーにした、そのRuntimeだけへの上書き |

個別の上書きが不要でも`"runtimes": {}`を残します。キーごと消すとエラーです。

特定のLambdaだけ値を変えたい場合は次のように書きます。Runtime名は`infrastructure/runtimes/`のディレクトリ名と一致させます。

```json
{
  "parameters": {
    "Environment": "dev",
    "ScheduleState": "DISABLED"
  },
  "runtimes": {
    "sample-batch-a": {
      "LogRetentionDays": "30"
    }
  }
}
```

### 書いてはいけない値

| 値 | 理由 |
| --- | --- |
| `AlarmTopicArn` | Deploy時にCI Stackの通知用Topic ARNで必ず上書きされる。書いても反映されない |
| Templateが宣言していないParameter名 | 綴り間違いを検出するため、エラーにする |
| `Environment`以外での環境名 | 環境の取り違えを防ぐため、`parameters`でのみ指定できる |

## 値をどこに書くか迷ったら

同じParameterでも、変わる単位によって置き場所が違います。下ほど優先されます。

| 変わる単位 | 置き場所 |
| --- | --- |
| 変わらない（全環境・全Runtime共通） | Runtime Templateの`Parameters`の`Default` |
| Runtimeごと（環境では変えない） | `../runtimes/<Runtime名>/parameters.json` |
| 環境ごと（全Runtime共通） | このディレクトリの`runtime-parameters.json`の`parameters` |
| 環境とRuntimeの組み合わせ | 同じファイルの`runtimes.<Runtime名>` |

実行スケジュールのようにRuntimeごとに決まる値は2番目、有効・無効のように環境で切り替える値は3番目が適切です。

## 書いた後の確認

AWSへ接続せずに確認できます。リポジトリルートで実行します。

```bash
python -m deployment.scripts.parameters --environment dev
python -m pytest infrastructure/tests
```

前者は`runtime-parameters.json`とRuntime側の設定を解決し、`sample-batch-a: parameters valid (7)`のように件数を表示します。値そのものは表示しません。

後者は`setup-parameters.json`のキー、形式、`Environment`とディレクトリ名の一致をTemplateの宣言と照合します。

## 関連

| 文書 | 内容 |
| --- | --- |
| [README](../../README.md) | 全体像、構築手順、Lambdaの追加 |
| [Infrastructure](../README.md) | Templateの設計。Runtime Stackの中身、IAMの考え方 |
| [Deployment](../../deployment/README.md) | この設定値を読む処理と、SAMへの渡し方 |
