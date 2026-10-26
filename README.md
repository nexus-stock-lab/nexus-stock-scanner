# NEXUS 米国株・仮想30トレード検証

実資金・注文・X投稿・外部通知の機能はありません。

## 今回の実装と状態

- scanner.py / review.py の候補抽出条件は維持しています。前営業日の完了日足による研究候補であり、23:45の正式判定そのものではありません。
- paper_engine.py は証拠を伴う登録、累計30件制限、同一銘柄重複防止、実際に観測された価格での損切り・利確・90日決済、区分別勝率・平均損益率・期待値R・平均保有日数を処理します。
- 旧 approved_entries.csv の YES と出典名だけでは登録しません。
- 決済追跡は欠落のない時刻付き取引履歴とその証拠が必要です。価格が飛んだ場合は指値・逆指値価格での約定を捏造せず、観測価格を使います。履歴欠落、分割未照合、時間順不明は保留します。
- 90日は暦日です。休日やデータ欠落時は期限超過を表示し、証拠が届くまでOPENです。期限後の最初の取引で決済する処理を用意していますが、期限時の約定規約は要確定です。

**完全自動運用は未完成です。** 正確な23:45価格提供元、公式業績・決算・下落理由の自動確認、根拠を伴う損切り・目標値の検証、業績シナリオ悪化による早期撤退、企業行動の調整が未接続です。未確認候補は0件登録のままにします。

## 23:45の価格

GitHub Actionsのcronは23:45開始を保証しません。quote_probe.py はYahooの1分足を取得して診断CSVと取得時刻を残します。23:45のラベルがあっても、その分のOHLCから23:45:00の取引価格は検証できません。このデータは正式約定の入力へ接続していません。

2026-10-27合意：正式判定時刻は23:45固定。23:45以前60秒以内の最後の有効取引を仮想基準価格とし、翌0:01以降に履歴で照合します。確認不能なら登録しません。price_policy.pyでこの方針を実装し、判定時刻と実際の取引時刻・検証時刻を分けて保存します。これは市場注文がその価格で成立した証明ではありません。

## iPhoneから

GitHub → Actions → NEXUS Market Scan → Run workflow。結果画面のSummaryで登録数を確認し、Artifactsのnexus-scan-resultsで詳細を確認できます。定期実行は日本時間の月〜金23:45です。mainへの反映後に有効になります。通知とX投稿は送信しません（GitHubアカウント側の既存通知設定は変更しません）。CSVの手編集は通常運用の前提にしていませんが、価格・調査アダプター接続までは完全自動の新規登録はできません。

台帳と証拠は input/trades.csv / input/evidence.json を同じコミットで永続化します。手動実行時も実行したブランチだけへ保存します。競合時はpush失敗を表示し、他の台帳を上書きしません。スキャン失敗でも、先に実行する保有追跡と台帳保存は独立しています。

## 入力契約（実装者向け）

将来の検証済みアダプターは output/verified_evidence.json に plans / quotes / streams を出力します。現在、そのファイルを生成する本番アダプターはありません。tests/test_paper_engine.py は架空データの契約例です。テストデータは台帳に書き込みません。

planはsymbol、signal（反発確認/反発初動）、decision_time_jst、prepared_at、stop_price、target_price、checksが必要です。checksの各項目はPASS、source、rationale、checked_atが必要で、判定時刻以前に準備されていなければ登録しません。quotesはsymbol、kind=trade、entry_policy=nexus_asof_60s_v1、decision_time_jst、market_time、retrieved_at、price、source、evidence_uriと、history_complete／sale_conditions_verified／latest_valid_trade_verified／provider_verifiedの明示的な検証結果が必要です。出典文字列だけで真実性を保証することはできません。信頼できるアダプターによる生データ保存と検証が必須です。

streamsはtrade_idごとのcoverage_start/end、complete、corporate_actions_clear、source、evidence_uri、tradesを持ちます。completeは欠落がないと検証できる提供元のみが設定してください。空の履歴で決済を作りません。

## 検証

`python -m unittest discover -s tests -v`

外部API不要の単体テストと、価格取得の実データ診断は区別します。単体テスト成功は価格提供元や自動判定完成の証明にはなりません。

## 2026-10-27 追加検証

証拠の同一バッチ内重複を除去。空銘柄、台帳の不正状態・不正価格・期限改変・損益計算不一致を検出して上書きを停止。Summaryに90日期限超過件数を追加。単体テスト14件成功。GitHub接続経由の書き込みは403。認証済みブラウザから作業ブランチへ保存し、ドラフトPRでレビューします。mainへの反映は承認待ちです。

## 時刻付き取引履歴の取得診断（未接続）

historical_probe.py は Alpaca Market Data API の歴史的SIP取引をGETで取得する診断部品です。注文・口座照会・送金・メッセージ送信は実装していません。既存の判定・約定処理には接続しません。

公式FAQでは無料利用時のSIP履歴照会はendが15分以上前である必要があります。実装では取得時刻を判定時刻の16分以上後に制限します。23:45の判定基準時刻は変更せず、履歴の問い合わせendも23:45に固定します。遅延して取得したことと、後の株価を使ったことを混同しません。ただし現在の履歴には後日訂正が反映され得るため、23:45当時に知られていた情報を再現できる保証はありません。

`python historical_probe.py --symbol AAPL --decision 2026-10-23T23:45:00+09:00`

環境変数ALPACA_DATA_KEYとALPACA_DATA_SECRETが必要です。キーをチャットやソースへ貼り付けないでください。実アカウントの認証付き取得は未検証で、アカウント利用資格・データ利用規約・アクセス権の確認が必要です。未設定ならネットワーク照会せずMISSING_CREDENTIALSを返します。有料契約は行いません。

診断窓は合意済みの直前60秒です。ページをすべて確認できなければINCOMPLETE_PAGINATION、範囲外・順序不正・不正価格はINVALID_RESPONSEです。生の応答をoutput/historical_probe_<sha256>.jsonへ保存し、ログに認証情報は出しません。ナノ秒を保持し、23:45を1ナノ秒でも過ぎた取引を範囲外にします。リダイレクト先へ認証情報を転送しません。

取得できても約定条件・取消・訂正は未検証なので自動合格にしません。verified_evidence.jsonは生成しません。23:45以前60秒以内の最終有効取引を仮想基準価格とする方針は合意済みです。実行可能な注文価格を保証するものではありません。

出典: https://docs.alpaca.markets/us/docs/market-data-faq / https://docs.alpaca.markets/us/reference/stocktradesingle-1

追加テスト10件、合計24件成功。認証付きAPI実測は未実施です。

## 合意済み価格ポリシーの実装

price_policy.pyはAlpacaの株式約定条件表に基づき、テープ別の分足終値更新対象を有効取引として扱います。奇数単位・参照価格・時間外等を除外し、未知の条件・同時刻の価格競合・履歴取得漏れは保留します。訂正・取消の突合と本番提供元の確認は未完了です。history_candidateはprovider_verified=Falseを返し、そのままでは台帳へ登録できません。

登録時にはentry_time_jst（23:45）、entry_market_time（実際の取引）、entry_verified_at（翌0:01以降）、registered_at_jst（実際の処理時刻）、entry_price_policyを別々に保存します。過去時点へ未確認約定を捏造しません。既存の業績・流動性・損切り・目標の根拠確認とR:R最低1.5は維持します。

単体テスト36件成功。APIの認証付き実測と本番アダプター検証は未実施。mainへの反映は未承認です。
