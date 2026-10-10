# Infrastructure

AWSへ作成するResourceの定義（Template）と、その設定値を置きます。

| パス | 内容 |
| --- | --- |
| `bootstrap/template.yaml` | CloudFormation Execution RoleとDeployment Bucket |
| `ci/template.yaml` | GitHub接続、Artifact Bucket、CodeBuild 3つ、Pipeline、通知 |
| `runtimes/<Runtime名>/template.yaml` | Runtime 1つ分（SAM Template） |
| `runtimes/<Runtime名>/parameters.json` | そのRuntimeの、全環境に共通する設定値 |
| `env/<環境>/` | 環境ごとの設定値（[書き方](env/README.md)） |
| `tests/` | Templateの構造と、Stack間の接続を確認するUnit Test |

Templateの中には日本語のコメントを書いています。JSONにはコメントを書けないため、設定値の説明は[env/README.md](env/README.md)にあります。

## Runtime Stackの中身

Runtime Stackは「まとめて配備したい範囲」を1つにしたものです。単独のLambdaなら、次のResourceが入ります。

```text
Runtime Stack: <Project名>-<環境>-<Runtime名>
│
├ Scheduler          AWS::Scheduler::Schedule      定期実行
│  └ Scheduler Role  AWS::IAM::Role                live Aliasのinvokeのみ
│        │ 呼び出し
│        ▼
├ Lambda             AWS::Serverless::Function
│  ├ Alias live      SAMが生成                     呼び出し先はここに固定
│  ├ Version         SAMが生成                     デプロイのたびに発行
│  ├ CodeDeploy      SAMが生成                     liveの参照先を切替
│  └ 実行Role        AWS::IAM::Role                自分のLog Groupへの書込のみ
│        │ 出力
│        ▼
├ Log Group          AWS::Logs::LogGroup           Stack削除後も保持
│
└ Alarm × 2          AWS::CloudWatch::Alarm        Errors / Duration
        └ Errors AlarmだけがCodeDeployの切替判定に使われる
```

Lambdaだけを別Stackにして周辺Resourceを共通化しない理由は2つです。

| 理由 | 内容 |
| --- | --- |
| 追いやすさ | 1つのTemplateを読めば、その処理に関わるResourceが全部分かる |
| 参照の制約 | Stackをまたいで値を渡すと、参照されている間はそのStackを作り直せなくなる |

## 3つのRuntimeの型

| 型 | サンプル | 起動 | 構成 |
| --- | --- | --- | --- |
| 単独のLambda | `sample-batch-a` | Scheduler | 上の図のとおり |
| ジョブ | `sample-flow-a` | Scheduler → State Machine | 関数3つ、State Machine、ジョブ単位の失敗Alarm |
| キュー処理 | `sample-queue-a` | SQS | Queue、DLQ、関数1つ、DLQ滞留Alarm |

新しいRuntimeは、近い型のディレクトリをコピーして作ります。変更するのは`CodeUri`だけです。

### ジョブ（sample-flow-a）

| 設定 | 採用値 |
| --- | --- |
| 関数ごとのResource | Log Group、実行Role、Version、live Alias（単独構成と同じ） |
| CodeDeploy Role | 3つの関数で1つを共用 |
| State Machine Role | このStackの3つのlive Aliasだけを呼び出せる |
| データの受け渡し | 前のステップの戻り値が次のステップの入力になる |
| リトライ | 各ステップで2回まで。5秒間隔 |
| 監視 | ジョブ単位の失敗Alarm 1つ。失敗箇所は実行履歴で特定する |

### キュー処理（sample-queue-a）

| 設定 | 採用値 | 理由 |
| --- | --- | --- |
| 起動 | Event Source Mapping（Lambdaがキューを見に行く） | 送信元にLambdaを呼ぶ権限が要らない |
| 呼び出し先 | live Alias | ほかの型と切替の単位をそろえる |
| 1回の件数 | 10件（`BatchSize`） | 呼び出し回数と1回の処理時間の釣り合い |
| 失敗の扱い | 失敗したメッセージだけキューへ戻す（`ReportBatchItemFailures`） | 成功した分まで再処理しない |
| 退避 | 3回失敗でDLQへ（`MaxReceiveCount`） | 処理できないメッセージの無限再試行を止める |
| 可視性タイムアウト | 180秒（Timeout 30秒の6倍） | 処理中のメッセージが再配信されないようにするAWSの推奨値 |

部分失敗の報告は、Templateの`FunctionResponseTypes`と関数の戻り値が**対で働きます**。片方だけでは無効です。

```python
return {"batchItemFailures": [{"itemIdentifier": "<失敗したmessageId>"}]}
```

Queueへメッセージを送る側（送信元）は、このStackでは管理しません。キューのARNを伝え、送信側に`sqs:SendMessage`を許可します。

## Resource名

Stack名は`<Project名>-<環境>-<Runtime名>`に固定し、その中のResource名もStack名を土台に付けます。

| Resource | 名前 |
| --- | --- |
| Lambda関数 | `<Stack名>`（ジョブでは`<Stack名>-fetch`のようにステップ名を付ける） |
| 実行Role | `<Stack名>-execution` |
| CodeDeploy Role | `<Stack名>-codedeploy` |
| Scheduler Role | `<Stack名>-scheduler` |
| Alarm | `<Stack名>-errors` / `-duration` / `-failed` / `-dlq` |
| SQS Queue | `<Stack名>` / `<Stack名>-dlq` |

名前を固定する理由は、**IAMで操作範囲を名前で絞るため**です。Log Groupだけは、Stack削除後も残す設定のため名前を固定せずCloudFormationに生成させます。

名前が上限を超えると途中が切り詰められ、名前で絞ったIAMの範囲から外れます。`tests/test_resource_names.py`が、Project名とRuntime名を上限いっぱいにした最長のStack名で、すべてのResource名が上限に収まるかを確認します。

Runtime名は20文字までで、`bootstrap`と`ci`は使えません（Stack名が他のStackと重なるため）。

## VersionとAlias

`AutoPublishAlias: live`を設定し、VersionとAliasをSAMに管理させます。独自のVersion発行やAlias更新の処理は持ちません。

```yaml
      AutoPublishAlias: live
      # コード以外の設定変更もVersion発行の対象にする
      AutoPublishAliasAllProperties: true
```

`AutoPublishAliasAllProperties: true`がないと、Timeoutなどの設定変更が`live`へ反映されません。「Templateに書いた内容 = liveの内容」を保つために有効にしています。

呼び出し側は必ず`live` Aliasを指します。Scheduler Roleに与える権限も、そのAliasの`lambda:InvokeFunction`だけです。`$LATEST`を直接呼ぶ経路は権限の側から塞いでいます。

### 変更しても新しいVersionが出ない場合

テストやドキュメントだけを変更したときは、関数の中身が変わらないためVersionは増えません。Deployは「変更なし」で成功します。異常ではありません。

逆に、**タグのように「Templateでは関数のプロパティだが、Lambdaの設定ではない」項目だけを変えると、デプロイが失敗します。**

```text
A version for this Lambda function exists ( 11 ). Modify the function to create a new version.
```

SAMは新しいVersionを作ろうとしますが、Lambdaはコードと設定が変わっていないVersionを発行しません。この場合は、コードか設定（`Description`、`MemorySize`、`Timeout`など）の変更を同じデプロイに含めます。

## CodeDeployによる切替

```yaml
      DeploymentPreference:
        Type: AllAtOnce
        Role: !GetAtt CodeDeployRole.Arn
        # このAlarmが鳴っている間は切り替えない
        Alarms:
          - !Ref ErrorsAlarm
```

`Alarms`に指定したAlarmが`ALARM`だと、CodeDeployは切替を行わずデプロイを失敗させます。**Alarmを作るだけでは連携されません。** この指定が必要です。

Duration Alarmは判定に含めていません。一時的な遅延でリリースが止まるのを避けるためです。

CodeDeploy Roleは、SAMに自動生成させず自分で定義しています。SAMに任せるとPathの無いRoleが作られ、Execution Roleの権限範囲から外れるためです。中身はSAMの既定と同じAWS管理ポリシーです。

### ジョブで自動Rollbackを設定しない理由

`sample-flow-a`では、各関数の`DeploymentPreference`にAlarmを関連付けていません。ジョブ単位の失敗Alarmは、State Machineを参照し、State Machineは各関数のAliasを参照します。これを切替の判定に使うと**循環参照**になり、CloudFormationが拒否します。

```text
Alarm → State Machine → Alias → Deployment Group → Alarm（循環）
```

影響は「切替直後にジョブが失敗しても自動では戻らない」ことです。各関数のErrors Alarmは通知として機能するため、異常には気付けます。戻す場合は前の正常なコミットへ戻す変更をpushします。

## 監視

| Alarm | 何を見るか | どのサンプルが持つか |
| --- | --- | --- |
| `<Stack名>-errors` | live Alias経由のエラー数（60秒で1件以上） | 全サンプル。切替判定にも使う |
| `<Stack名>-duration` | 実行時間の最大値（Timeoutの8割） | `sample-batch-a` |
| `<Stack名>-failed` | ジョブの失敗（`AWS/States`） | `sample-flow-a` |
| `<Stack名>-dlq` | DLQの滞留（`AWS/SQS`） | `sample-queue-a` |

Errors AlarmのDimensionsには`FunctionName`と`Resource: <関数名>:live`を指定し、**Alias経由の呼び出しだけ**を対象にしています。

`TreatMissingData: notBreaching`を指定しているため、実行がない期間は異常として扱いません。低頻度の定期実行ではデータ欠損が普通です。ただし裏返すと、**「動いていないこと」はこのAlarmでは検知できません**。

通知はすべて、CI Stackが作る1つのSNS Topic（`<Project名>-<環境>-operations`）へ送ります。購読先はTemplateで管理しません。

## IAMの考え方

権限を2つのRoleに分けています。

```text
CodeBuild（Deploy Role／CI Stack）
   │ sam deploy --role-arn <Execution RoleのARN>
   ▼
CloudFormation
   │ 渡されたRoleを引き受けて実行
   ▼
Execution Role（Bootstrap Stack）
   │
   ▼
Lambda / IAM Role / Log Group / Alarm / Queue を作成
```

| Role | できること | できないこと |
| --- | --- | --- |
| Deploy Role | Change Setの作成・実行、S3への資材アップロード | Lambdaを直接作る、IAM Roleを直接作る |
| Execution Role | Runtime StackのResourceを作る | 自分から動く（CloudFormationしか引き受けられない） |

Deploy Roleが別のRoleを渡せないよう、`cloudformation:RoleArn`の条件でExecution Roleに固定しています。あわせて、Bootstrap / CI Stack自身へのChange Set操作は明示的に拒否しています。

### 操作できる範囲の絞り方

| 対象 | 絞り方 |
| --- | --- |
| Lambda、Alarm、Schedule、Queue等 | 名前の前方一致（`<Project名>-<環境>-*`） |
| IAM Role | Path（`/<Project名>/<環境>/runtime/`） |
| `iam:PassRole` | 上記PathのRoleかつ、渡す先のサービスを4つに限定 |

IAM RoleをPathで絞ると、Runtime TemplateからCI StackのRoleやExecution Role自身の権限を変更できません。

Actionの書き方は、影響の大きさで2通りに分けています。

| 方式 | 対象 | 理由 |
| --- | --- | --- |
| Actionを列挙 | Lambda、IAM、SQS、S3、CloudFormation | 関数の公開設定、Role・Policyの変更、メッセージ操作など影響が大きい操作を含むため |
| Service単位（`<service>:*`） | Logs、CloudWatch、CodeDeploy、Step Functions、Scheduler | 同じResourceへの操作しか増えず、設定追加のたびにBootstrap Stackを更新せずに済むため |

**権限の広さを決めているのはActionの数ではなくResourceの限定です。** どのStatementも名前かPathで範囲を絞っています。

Resource ARNで範囲を絞れないActionは、別のStatementへ分けています（`logs:DescribeLogGroups`、`codedeploy:CreateCloudFormationDeployment`、Event Source Mappingのタグ操作）。

## タグ

`Project` / `Environment` / `ManagedBy`の3つを、**Stackに付けてCloudFormationに伝播させます。** Templateの各Resourceには書きません。書き漏れたResourceだけタグが欠ける状態を避けるためです。

| Stack | 付け方 |
| --- | --- |
| Bootstrap / CI | 管理者が`aws cloudformation deploy --tags`で指定 |
| Runtime | Deploy Stageの`sam deploy --tags`が自動で付ける |

CloudFormationがタグを扱えないResource（`AWS::Scheduler::Schedule`、Lambda VersionとAlias）には付きません。

## 共有コードの同梱

`application/common/`のコードは、ビルド後に各Lambdaのパッケージへコピーされます。**Lambda Layerは使いません。**

| 理由 | 内容 |
| --- | --- |
| 依存の追跡 | Layerのバージョンと関数の対応を別に管理する必要がない |
| Versionの一致 | 共有コードを変えると全Lambdaが新しいVersionを発行し、実際に動くコードとVersionが一致する |

同梱は`deployment/scripts/build.py`が行います。`sam build`を単体で実行したパッケージには共有コードが入らないため、完全なパッケージが必要な場合はビルド処理を使います（[deployment/README.md](../deployment/README.md)）。

## Templateの検証

AWSへ接続せずに確認できます。

```bash
# 書式とSAMの変換（SAM CLIが必要）
sam validate --lint --region ap-northeast-1 --template-file infrastructure/bootstrap/template.yaml
sam validate --lint --region ap-northeast-1 --template-file infrastructure/ci/template.yaml
sam validate --lint --region ap-northeast-1 --template-file infrastructure/runtimes/sample-batch-a/template.yaml

# 構造とStack間の接続
python -m pytest infrastructure/tests
```

`tests/`が確認しているのは主に次の点です。

| テスト | 確認内容 |
| --- | --- |
| `test_bootstrap_template.py` | Execution Roleの範囲、Service単位の許可が低リスクな5サービスに限られていること |
| `test_ci_template.py` | Stageの構成、Roleの分離、Bucketの設定 |
| `test_runtime_templates.py` | Runtime Templateの共通構造 |
| `test_resource_names.py` | 最長のStack名でも名前が上限に収まること |
| `test_stack_contract.py` | Stack間・Stage間で受け渡す名前が食い違っていないこと |
