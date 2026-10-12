# ブランチ運用

簡易版の git-flow で運用する。

| ブランチ | 役割 | 切り元 | マージ先 |
|---|---|---|---|
| `master` | 本番。push されると本番の Bot にデプロイされる | - | - |
| `develop` | 開発中の変更を集める。GitHub の既定ブランチ | `master` | `master` |
| `feature/<内容>` | 機能追加・修正・リファクタ | `develop` | `develop` |
| `hotfix/<内容>` | 本番で起きた不具合の緊急修正 | `master` | `master` と `develop` |

`master` は保護されており、直接 push や force push はできない。必ず PR を通してマージする。

## 普段の開発

```sh
git switch develop
git pull
git switch -c feature/inmu-voice   # 内容が分かる名前にする

# 作業してコミット
git push -u origin feature/inmu-voice
gh pr create --base develop
```

PR を `develop` にマージしたら、ブランチは削除する。

## 本番へのリリース

`develop` で動作を確認できたら、`develop` から `master` への PR を作ってマージする。
マージすると本番にデプロイされる。

```sh
gh pr create --base master --head develop --title "Release: <主な変更>"
```

## 緊急修正

本番で不具合が起きて、`develop` にある未リリースの変更を待てないときに使う。

```sh
git switch master
git pull
git switch -c hotfix/<内容>
# 修正してコミット
git push -u origin hotfix/<内容>
gh pr create --base master
```

`master` にマージしたら、同じ修正を `develop` にも取り込む。

```sh
git switch develop
git pull
git merge origin/master
git push
```

## コミットメッセージ

`種類: 内容` の形で、日本語で書く。

- `Feat:` 機能追加
- `Fix:` 不具合の修正
- `Refactor:` 動作を変えないコードの整理
- `Docs:` ドキュメントのみの変更
