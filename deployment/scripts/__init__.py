"""Command-line helpers used by local development and CodeBuild."""

# このパッケージには、ローカル開発とCodeBuildの双方から実行するスクリプトを置きます。
# いずれもリポジトリルートからPython moduleとして実行します。
#   parameters.py … Parameterの読込・検証・解決（AWSへは接続しません）
#   build.py      … 全RuntimeのビルドとBuildArtifactの生成（配備はしません）
#   deploy.py     … BuildArtifactの検証とsam deployによる配備
