# 不動産調査ツール β版

住所を入力すると、**用途地域・ハザードリスク・周辺取引価格**を自動取得して表示する不動産業者向けデモWebアプリです。

## 機能

| 機能 | データソース | APIキー |
|------|------------|--------|
| 住所→緯度経度変換 | 国土地理院 AddressSearch API | 不要 |
| 逆ジオコーディング（市区町村コード取得） | 国土地理院 逆ジオコーダAPI | 不要 |
| 用途地域（建蔽率・容積率） | 国土交通省 不動産情報ライブラリ | **必要（無料）** |
| 洪水・津波・土砂災害リスク | 国土交通省 不動産情報ライブラリ | **必要（無料）** |
| 周辺不動産取引価格 | 国土交通省 土地総合情報システム | 不要 |

> **注意**: 取引価格は APIキーなしで取得できます。用途地域・ハザード情報は無料登録が必要です。

---

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. (オプション) 不動産情報ライブラリ APIキーの取得

用途地域・ハザード情報を表示するには、無料APIキーが必要です。

1. [不動産情報ライブラリ](https://www.reinfolib.mlit.go.jp/) にアクセス
2. 無料ユーザー登録を行い、APIキーを発行
3. 環境変数に設定:

```bash
# Windows (PowerShell)
$env:MLIT_API_KEY = "your_api_key_here"

# macOS / Linux
export MLIT_API_KEY="your_api_key_here"
```

### 3. サーバー起動

```bash
uvicorn main:app --reload
```

### 4. ブラウザでアクセス

```
http://localhost:8000
```

---

## 使い方

1. 住所テキストボックスに調査したい住所を入力（例: `三重県いなべ市`、`東京都千代田区丸の内`）
2. 「調査する」ボタンをクリック
3. 地図上にピンが立ち、3枚のカードに情報が表示されます

---

## ディレクトリ構成

```
realestate-demo/
├── main.py          # FastAPI バックエンド
├── requirements.txt
├── README.md
└── static/
    └── index.html   # フロントエンド（単一HTMLファイル）
```

---

## 使用API一覧

| API | エンドポイント |
|-----|--------------|
| 国土地理院 住所検索 | `https://msearch.gsi.go.jp/address-search/AddressSearch` |
| 国土地理院 逆ジオコーダ | `https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress` |
| 不動産情報ライブラリ 用途地域 | `https://www.reinfolib.mlit.go.jp/ex-api/external/XKT010/` |
| 不動産情報ライブラリ 洪水浸水想定区域 | `https://www.reinfolib.mlit.go.jp/ex-api/external/XKT007/` |
| 不動産情報ライブラリ 津波浸水想定 | `https://www.reinfolib.mlit.go.jp/ex-api/external/XKT011/` |
| 不動産情報ライブラリ 土砂災害警戒区域 | `https://www.reinfolib.mlit.go.jp/ex-api/external/XKT012/` |
| 土地総合情報システム 取引価格 | `https://www.land.mlit.go.jp/webland/api/TradeListSearch` |

---

## Cloud Run へのデプロイ

```bash
gcloud run deploy realestate-demo \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --set-env-vars MLIT_API_KEY=your_api_key_here
```

---

## データ出典

- [国土交通省 不動産情報ライブラリ](https://www.reinfolib.mlit.go.jp/)
- [国土地理院](https://www.gsi.go.jp/)
- [国土交通省 土地総合情報システム](https://www.land.mlit.go.jp/webland/)
