# UDP画面配信

## 目的と対象

comma 3Xのsunnypilot UIを同一Wi-Fiの受信端末へ配信する。送信側は汎用的な画面配信機能とし、MIB・MOST・AIDへの依存を持たせない。受信側の候補はFFplayとVcMOSTRenderMqbのstream-player。実機からPCへの受信結果は利用者からの報告に基づく。本修正後の実機確認とMIB側の操作は、この開発環境では実行していない。

## ブランチ構成

```text
upstream/master
   ↓
udp-screen-streaming  開発・将来のPR用
```

起点は`upstream/master`の`a5f44653d7f43ad57fef2f546f3916ec4cbf3c56`。実機テスト用ブランチとcherry-pick先は未定。

以後の開発では次の順序を使う。以下は開発PCでの操作。

```sh
git switch udp-screen-streaming
git fetch upstream
git rebase upstream/master
# 実装・検証・コミット後
git push origin udp-screen-streaming
```

履歴が書き換わった開発ブランチのpushが拒否された場合は、共同作業者の変更を確認してから対応する。

## 実装

- `system/ui/lib/screen_stream.py`: 接続Wi-Fiの取得、FFmpegプロセス管理、最新フレームの待ち行列、書き込み期限、再試行。`MpegTsUdpSender`が標準出力の読み取り・TS分割・Python socketでの送信を担当する。
- `system/ui/lib/screen_capture.py`: 録画と共有するRGBA読み出し、GPUでの縮小、20fpsへの間引き。
- `system/ui/lib/application.py`: 必要時のみRenderTextureを確保し、画面描画後に配信キャプチャを行う。`RECORD=1`の場合は配信ワーカーを起動しない。
- `selfdrive/ui/ui.py`: 通常UIから配信機能を登録。他のGUIツールや録画専用ツールには配信を自動登録しない。
- `selfdrive/ui/layouts/settings/toggles.py`: comma 3X向け設定スイッチ。小画面UIにも同じ設定を追加。
- `common/params_keys.h`: 永続BOOLの`ScreenStreamEnabled`を初期値`0`で登録。バックアップから意図せず有効化されないようBACKUP対象にはしない。

上記パスはリポジトリ内の`openpilot/`配下。UIの原文は英語で、本家標準の`tr`／`tr_noop`とPOカタログにより既存の12言語へ翻訳する。フォントは本家の言語別フォント切替を使用する。追加コメント・手順書は日本語、READMEは英日併記。

### 配信設定と多言語UI

トグルの原文は`UDP Screen Streaming`で、日本語選択時は「UDP画面配信」と表示する。ONにすると直下へ以下の4項目を表示する。Wi-Fi未接続で送信待機中でも設定可能。`RECORD=1`の録画中は編集できない。

| 英語UI | 永続キー | 既定値 | 入力範囲 |
| --- | --- | --- | --- |
| Destination Address（送信先アドレス） | `ScreenStreamAddress` | 239.255.42.99 | 通常のIPv4 unicast、またはmulticastの224.0.1.0～239.255.255.255 |
| UDP Port | `ScreenStreamPort` | 12346 | 1～65535 |
| Bitrate (kbit/s) | `ScreenStreamBitrate` | 1500 | 250～8000 |
| Multicast TTL | `ScreenStreamTtl` | 1 | 1～255 |

アドレスは`192.168.4.44`、`10.0.0.20`などの通常のunicastと、従来のmulticastを受け付ける。0.0.0.0と0/8、127/8、link-local、予約済みIPv4、255.255.255.255、224.0.0/24、IPv6、ホスト名、URL、ポートやクエリ付き入力は拒否する。サブネットごとのdirected broadcast判定は行わないため、受信端末のホストアドレスを指定する。TTL欄は表示を維持し、説明にmulticast専用・unicastでは無視と明記する。TTLの保存範囲は従来どおり1～255で、multicastで同一ネットワークなら1を使う。既存Param名と既定値は変更しない。設定は再起動後も保持し、OFFにしても値を消去しない。解像度800×480と最大20fpsは固定。

`screen_stream_config.py`の検証をUI入力と配信バックエンドで共用する。無効な入力は保存せず、翻訳されたエラーと入力範囲を表示して再入力を求める。キャンセル時は値を変更しない。設定は通常1秒周期のワーカー側チェックで反映し、変更時に古いFFmpeg・送信スレッド・socket・待機フレームを解放し、エンコーダと送信処理を再生成する。本体UIの再起動は不要。新しいキーを登録するため、この更新を初めて導入するときはネイティブライブラリの再ビルドが必要。

共通UIは`system/ui/widgets/screen_stream_settings.py`に置き、comma 3Xでは標準のリストとKeyboard、小画面UIではBigButtonとBigInputDialogを使用する。翻訳の原文は同ファイルの`tr_noop`で抽出でき、テンプレートは`selfdrive/ui/translations/app.pot`、翻訳は`app_*.po`へ格納する。配信設定の文字列は12言語すべてに翻訳を収録する。以後の更新は本家の`python -m openpilot.selfdrive.ui.translations.update_translations`を使用できる。

### キャプチャ

```text
通常UIのRenderTexture
  ├─ 本体画面
  └─ GPUで800×480へ縮小（縦横比保持・黒帯）
       └─ RGBA読み出し → 最新1フレーム → FFmpeg stdin
            → libx264 / MPEG-TS → stdout (pipe:1)
            → 専用送信スレッド → Python socket → UDPユニキャスト／マルチキャスト
```

2160×1080のフル画面をCPUへ読み出す代わりに800×480で読み出す。20fps時の生RGBA転送量は約30.72 MB/sで、フル解像度の約186.62 MB/sより小さい。これは画素数からの計算で、実測値ではない。

OpenGLの上下方向は縮小描画時とFFmpegの`vflip`で揃える。FPS表示・タッチ位置など、画面転送後のデバッグ表示は配信に含まない。元のUIが低フレームレートのときに不足フレームを再生したり複製したりしない。

### エンコードと送信

固定設定は800×480、最大20fps、libx264 baseline / yuv420p、Bフレームなし、GOP 10、`veryfast`、`zerolatency`。ビットレートは設定値を使い、VBVはその約1/3とする。SPS/PPSを繰り返し、MPEG-TSで送信する。既定値は1500 kbit/s、VBV 500 kbit、宛先`239.255.42.99:12346`、TTL 1。UDPペイロードはPython側で最大1316 bytesに制限する。FFmpeg URLの`pkt_size`オプションは使用しない。

Wi-Fiの判定は、NetworkManagerの接続済みWi-Fiデバイス、インフラストラクチャモード、有効なIPv4の組み合わせで行う。デフォルトルートが携帯回線でもWi-Fiが接続済みなら配信できる。宛先を`ipaddress.IPv4Address(config.address).is_multicast`で判定する。両方式とも`socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)`、非ブロッキング、`sendto(payload, (config.address, config.port))`を使い、`connect()`と`SO_BINDTODEVICE`は使わない。multicastは従来どおり`IP_MULTICAST_IF`へWi-Fi IPv4、`IP_MULTICAST_TTL`へ設定TTLを指定し、bindしない。unicastは`bind((local_address, 0))`で送信元Wi-Fi IPv4を指定し、multicast用のsocket optionを一切設定しない。unicastのIP TTLはOSの既定値を使う。Wi-Fiインターフェース名・IPv4・宛先・ポート・ビットレート・TTLのいずれかが変われば、FFmpegとsocketを作り直す。

UIスレッドはFFmpegへ直接書かない。ワーカーが非ブロッキングパイプを使い、`FRAME_MAX_AGE=0.25`秒を超えた未送信フレームは単に破棄し、再起動しない。実際の書き込み開始時刻から`PIPE_WRITE_TIMEOUT=0.50`秒を設け、その期限を超過した場合に再起動する。待ち行列での待機時間をパイプの書き込み猶予から差し引かない。書きかけのrawvideoを途中で捨てて次フレームへ継ぎ足すことはしない。ワーカーのスケジューラとniceを変更した後にFFmpegと送信スレッドを生成し、UIのリアルタイム優先度を継承させない。エンコーダのスレッド数も2に制限する。

設定確認は`CONFIG_INTERVAL=1.0`秒、Wi-Fi確認は接続中`NETWORK_INTERVAL_CONNECTED=5.0`秒・未接続時`NETWORK_INTERVAL_DISCONNECTED=1.0`秒に分離する。次の照会は前回の完了時刻から間隔を空ける。OFF・消灯は通常100ms以内に確認し、処理中の照会・書き込み・子プロセス終了処理分の遅延が加わる。エンコーダや送信側の障害後は約3秒待って再試行する。Wi-Fi照会の一時失敗にはこの3秒待機を適用しない。これらは処理の期限であり、端末間の映像遅延を保証する値ではない。

OFF時はWi-Fi照会・FFmpeg・socketを動作させず、配信専用のGPUリソースを解放する。表示用・録画用に必要なRenderTextureは維持する。

### FFmpegのパイプ出力と送信スレッド

実機から報告されたcomma 3Xの標準FFmpegは、入力・出力ともに`file`と`pipe`のみを提供する。`libx264`と`mpegts`は利用できるが、FFmpegの`udp`プロトコルは利用できない。このためエンコードと送信を分離する。

```text
FFmpeg出力の変更前: udp://ADDRESS:PORT?pkt_size=1316&ttl=TTL&localaddr=LOCALADDR
FFmpeg出力の変更後: pipe:1
```

`ffmpeg_command(config)`は送信元アドレスを引数に取らず、宛先・ポート・TTLもコマンドへ渡さない。エンコード条件は維持する。標準入力・標準出力を`subprocess.PIPE`、`bufsize=0`で開き、stderrは従来どおり継承する。

ワーカーはRGBAをstdinへ書き、別の送信スレッドがstdoutを常時読み出す。読み書きを同じスレッドで処理しないため、stdoutの満杯でエンコーダが停止し、stdin書き込みまで停止する循環待ちを防ぐ。stdoutとsocketは非ブロッキングで処理する。送信バッファの空きを待たず、一時障害のデータグラムは再送しない。

読み取り境界とTSパケット境界は一致しない。読み取ったデータを一時バッファへ追記し、1316 bytes（188×7）ずつ順序どおり`sendto()`する。1316 bytes未満は次の読み取りへ保持し、正常EOF時だけ188 bytesの整数倍の残りを送信する。停止時の末尾と188 bytes未満の端数は破棄する。送信障害がない場合、完全なTSパケットに欠落・重複・並べ替えを加えない。一定のTS出力に対する1316 bytes蓄積時間は500 kbit/sで約21ms、1500 kbit/sで約7msだが、これは最大時間の保証ではなく、実際の出力が途切れれば蓄積待ちは長くなる。

一時的な送信エラー（`BlockingIOError`、`EAGAIN`、`EWOULDBLOCK`、`ENOBUFS`、`EINTR`、`TimeoutError`とWindowsの対応コード）は、そのデータグラムだけを破棄して送信を続行する。`datagrams_sent`、`datagrams_dropped`、`bytes_sent`を記録し、破棄の警告は初回と以後5秒間隔に集約する。`ENETDOWN`、`ENODEV`、`EADDRNOTAVAIL`などの致命的な障害は保持してワーカーへ通知し、元のerrnoと例外のreprをcloudlogへ出す。stdoutのEOFも停止として検出する。障害時はreadyを解除して送信停止を通知し、FFmpegをterminate、200msで終了しなければkillしてさらに200ms待つ。stdin/stdoutを閉じ、送信スレッドを最大500msでjoinし、socketとフレーム待ち行列を解放する。約3秒後に再試行する。消灯やOFFでも同じ後片付けを行い、点灯・ON時は自動再開する。

## comma 3Xでの準備

実機テスト用ブランチと導入先の選定後に、停車・車両電源OFFの状態で作業する。現在のブランチとコミット、作業差分を記録し、未保存の変更がある場合は先に保存する。

```sh
cd /data/openpilot
git status --short
git branch --show-current
git rev-parse HEAD
git remote -v
```

配信の有効化と送信設定の5つのキーはC++のパラメーターライブラリにも登録されるため、Pythonファイルだけの置き換えでは動作しない。配布用stagingのビルド済みライブラリを使う場合も、新しいキーを含めたソースの再ビルドが必要。

導入するソースを確定後、サブモジュールとLFSの取得を完了し、端末の通常の起動処理でビルドする。手動で確認する場合は本家の端末用Python環境を有効にした上で、`scons -j2`を実行する。Windows環境の`.venv`は端末へコピーしない。ビルドが正常終了してからUIを起動する。

ビルド後に新規パラメーターが認識されることと、FFmpegの機能を確認する。

```sh
python -c 'from openpilot.common.params import Params; print(Params().get_bool("ScreenStreamEnabled"))'
ffmpeg -hide_banner -encoders | grep libx264
ffmpeg -hide_banner -protocols | grep pipe
ffmpeg -hide_banner -muxers | grep mpegts
```

送信側のFFmpegに`udp`プロトコルは不要。設定の読み取り・Wi-Fi判定・エンコーダ起動は次のコマンドでも確認できる。配信をONにして画面を点灯させてからプロセスを確認する。

```sh
python -c 'from openpilot.common.params import Params; from openpilot.system.ui.lib.screen_stream import wifi_address; from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig; p = Params(); print(p.get_bool("ScreenStreamEnabled")); print(ScreenStreamConfig.from_params(p)); print(wifi_address())'
pgrep -a -x ffmpeg
```

FFmpegの出力先が`pipe:1`であり、プロセスがすぐに終了しないことを確認する。受信PCではREADMEのFFplayコマンドで映像を確認する。受信側FFplayのUDP機能は引き続き必要。OFFや消灯でFFmpegが終了し、再開時に復帰することも確認する。

設定画面から「UDP Screen Streaming」（日本語では「UDP画面配信」）をONにする。SSHから設定する場合は次の操作が同等。ONにすると設定画面を含め同じLANへ画面が公開される。

```sh
python -c 'from openpilot.common.params import Params; Params().put_bool("ScreenStreamEnabled", True, block=True)'
```

OFFへ戻すには`True`を`False`へ変更する。ブランチのロールバックは、記録した元のブランチへ切り替え、サブモジュールを同期して再起動する。

multicastでは受信URLにも同じグループアドレス・ポートを指定する。unicastでは本体の送信先にPCのWi-Fi IPv4を設定し、PCは同じポートで待ち受ける。

## 実機での再起動診断

現在の実機`9f0d5360e`ではPIDの周期的再起動は解消済み。利用者の集計は`sent=21536 dropped=0 bytes_sent=28341376`。一方、受信PCのPacket corrupt・H.264復号エラーと約1000msの遅延が残っている。以下の再起動原因の説明は解消済み問題の診断記録であり、今回もその対策を維持する。

利用者からは、comma 3XからPCへH.264 Constrained Baseline / yuv420p / 800×480 / 20fpsの映像が届く一方で、受信側のTS破損・DTS順序エラーとFFmpeg PIDの周期的変化が報告されている。Wi-Fi tupleと設定は30回の照会で安定し、単独stdinベンチマークでは50フレーム中の最大書き込み時間が114.2msだった。実機cloudlogで、大半の再起動理由が`network query error: TimeoutError(...)`と判明した。複数のD-Bus往復で250msの共通期限を消費し、一時的な照会失敗でも正常な配信を停止していたことが主因である。UDP破棄ログは報告されておらず、実際のビットレート変更による再生成は正常な動作として区別する。

`wifi_address()`はGetDevices・Device.GetAll・Wireless.GetAll・AddressDataの各往復へ`NETWORK_REQUEST_TIMEOUT=0.25`秒を独立に与える。認証の期限も250ms。1回の問い合わせ全体を250msへ押し込まず、個々の待機は制限する。

問い合わせが例外で失敗した場合は、最後に正常取得したWi-Fi tupleを変更せず、FFmpeg・sender・readyを維持する。起動前でtupleがない場合はエンコーダを起動せず、約1秒後の照会を待つ。どちらも`_restart_count`を増やさず、無意味なプロセス終了処理を行わない。設定取得は独立に行うため、ネットワーク照会失敗中もビットレートなどの変更を反映できる。

正常に`None`が返った場合は、確認済みの未接続としてエンコーダを停止し、次の接続を待つ。正常に異なるtupleが返った場合は、送信インターフェースを更新するためエンコーダとsenderを再生成する。設定変更・tuple変更は通常の状態変更であり、障害再起動の回数には含めない。

ログは`openpilot.common.swaglog.cloudlog`を使用し、通常のopenpilot環境ではlogmessaged経由で保存する。journalctlだけでは見えない場合がある。

```sh
# 実際のFFmpeg PIDとコマンド
pgrep -a -x ffmpeg
watch -n 0.2 'pgrep -a -x ffmpeg'

# ネットワーク照会の失敗と復旧、実際の障害再起動、起動情報、UDP破棄の集計
grep -R -a "screen stream network query" /data/log 2>/dev/null | tail -50
grep -R -a "screen stream restart" /data/log 2>/dev/null | tail -50
grep -R -a "screen stream started" /data/log 2>/dev/null | tail -20
grep -R -a "screen stream UDP drops" /data/log 2>/dev/null | tail -20
```

| ログ | 内容 |
| --- | --- |
| `screen stream started:` | PID、`mode=unicast/multicast`、Wi-Fi tuple、宛先、ビットレート、TTL（unicastでは未使用） |
| `screen stream stopped:` | PID、終了コード、停止理由（`stream disabled`、`screen invisible`、`shutdown`など） |
| `screen stream restart: ffmpeg exited` | FFmpeg自身の終了と終了コード |
| `screen stream restart: frame pipe broken` | stdin破損、errno、終了コード、例外 |
| `screen stream restart: frame write timeout` | フレームage、書き込み開始からの経過時間、残りbytes |
| `screen stream restart: sender fatal error` | 元のerrnoと例外のrepr。生成時の失敗も区別 |
| `screen stream restart: sender EOF` | stdoutの終了 |
| `screen stream restart: invalid config` | 設定読み取りの失敗 |
| `screen stream restart: encoder start failed` | FFmpeg欠落などの起動失敗 |
| `screen stream state: network changed` / `config changed` | 正常な再生成の理由と変更前後の値 |
| `screen stream network query transient:` | 連続照会失敗数、維持するWi-Fi tuple、例外のrepr。最初の1回と以後10秒間隔 |
| `screen stream network query failed before start:` | 起動前の照会失敗。約1秒周期で再照会し、警告は最初の1回と以後10秒間隔 |
| `screen stream network query recovered:` | 照会成功時に1回記録し、連続失敗数をリセット |
| `screen stream state:` | 起動・復帰時の状態変更、Wi-Fi未接続 |
| `screen stream UDP drops:` | 累積破棄数、成功数、最後のerrno。初回と以後5秒間隔 |
| `screen stream sender stopped:` | 送信成功数、破棄数、送信bytesの最終集計 |

再起動ログの`count=`はワーカー内の障害再起動回数で、UIプロセスを起動し直すとリセットする。手動OFF・消灯・確認済みの未接続による停止、設定やWi-Fi tupleの変更、一時的なWi-Fi照会失敗は含めない。安定した周期照会ではログを増やさず、一時照会失敗のたびにstack traceは出さない。想定外の例外は`screen stream restart: unexpected error`、ワーカー自体の終了は`screen stream worker failed`で記録する。

古いフレームの破棄、一時的なUDP送信失敗、D-Bus照会の一時失敗ではFFmpegを再起動しない。ネットワーク・設定変更では送信側とエンコーダを再生成する。OFF・消灯では停止し、ON・点灯で再開する。

パケット破棄は映像欠損として見える可能性がある。GOP 10 / 20fpsならIDRの間隔は約0.5秒だが、継続的な通信損失や受信側の状態によって復旧は遅れる。`9f0d5360e`で周期的再起動は解消しPID安定と報告されている。方式切替時の正常な再生成と、試験中の予期しない再起動を区別して記録する。

照会失敗が長時間続く場合も最後に確認できたWi-Fi情報を保持するため、実際の切断やIP変更の検出が遅れる可能性がある。正常応答が戻れば結果を反映し、socket側の致命的障害は従来どおり別経路で検出する。接続中の変更検出は最大約5秒に照会所要時間が加わる。D-Bus照会はUIではなく配信ワーカーで行うため、その実行中は新しいRGBAフレームの書き込みが一時停止し得る。期限は1リクエストごとで、照会全体の固定上限ではない。

## 受信と切り分け

### 通常受信と低遅延テスト

PCのWi-Fi IPv4を確認する（Windowsなら`ipconfig`）。以下は`192.168.4.44`の場合。受信側のFFplayにはUDPとH.264/MPEG-TS対応が必要。各コマンドは1行のままPowerShellでも実行できる。FFplayは一度に1プロセスだけ起動し、切替時は終了させる。

| 方式 | commaのDestination Address | PCの受信URL |
| --- | --- | --- |
| A: multicast | `239.255.42.99` | `udp://239.255.42.99:12346?localaddr=192.168.4.44` |
| B: unicast | `192.168.4.44` | `udp://0.0.0.0:12346` |

`0.0.0.0`はPC側の待受URLだけに使い、本体の送信先には設定しない。multicastの`localaddr`もPC自身のWi-Fi IPv4であり、commaのIPではない。

通常受信:

```sh
ffplay "udp://239.255.42.99:12346?localaddr=192.168.4.44"
ffplay "udp://0.0.0.0:12346"
```

低遅延テスト:

```sh
ffplay -max_delay 0 -avioflags direct -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://239.255.42.99:12346?localaddr=192.168.4.44"
ffplay -max_delay 0 -avioflags direct -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
```

特定インターフェースでのunicast待受には`udp://0.0.0.0:12346?localaddr=192.168.4.44`を使う。Windowsでも`udp://192.168.4.44:12346`を比較できるが、ビルド差を避けるため明示的な`localaddr`を基本とする。[FFmpegのUDP実装](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/udp.c)では受信時のローカルアドレスを解決してbindする。

比較では`fifo_size`と`overrun_nonfatal`を指定せず、受信FIFOを既定値に戻す。スレッド対応ビルドの既定FIFOは188 bytes×7×4096で、容量は常にその分の遅延が生じることを意味しない。必要になった場合だけ別試験で変更する。[FFmpeg UDP仕様](https://ffmpeg.org/ffmpeg-protocols.html#udp)

`avioflags=direct`はバッファリングを減らし、`fflags=nobuffer`は初期入力解析中のバッファリングを減らす。`max_delay`はmux/demuxの最大遅延をマイクロ秒で指定する。[FFmpeg形式オプション](https://ffmpeg.org/ffmpeg-formats.html#Format-Options)。`max_delay=0`でUDPの並べ替え待ちを無効化する説明は公式資料の[RTSP節](https://ffmpeg.org/ffmpeg-protocols.html#rtsp)にある。今回のraw MPEG-TS/UDPで同じ待ちが発生しているとは断定せず、効果を個別に測定する。これらは受信FIFOを一律に無効化する設定ではない。

`probesize`はまず4096とし、32へ極端に下げない。映像が検出できない場合は通常受信へ戻して確認する。小さな解析量やフレーム破棄には、検出失敗・表示の欠落とのトレードオフがある。遅延短縮量は受信ビルドと表示環境を含む実測で判断する。

### multicast / unicastのA/B試験

1. commaとPCを同じWi-Fiへ接続する。機器位置、AP、PCの電源設定・表示リフレッシュレート、FFplayのバージョンを固定する。画面点灯を維持し、同じUI操作を繰り返す。
2. commaで配信ON、800×480・20fps・GOP 10・B-frame 0のまま、ビットレートを500 kbit/sへ設定する。ポート12346、TTL 1とする。FFmpegのエンコード設定は変更しない。
3. 配信ONの状態で送信先Aを保存してからOFFにする。受信側をAの通常受信で起動し、配信ONにする。`screen stream started`のmode・宛先・PIDを確認する。
4. 起動から10秒をウォームアップとして区別し、その後5分間測定する。ログ・映像・時刻を保存して配信OFFにし、`screen stream sender stopped`の最終集計を記録する。集計にはウォームアップも含まれるため、全試験で時間を揃える。
5. 受信側を終了し、送信先をBへ変更してBの通常受信で同じ試験を行う。方式切替に伴うPID変更は正常。各5分間の試験中のPID変化を別に数える。
6. A/Bを最低3回繰り返し、順序も入れ替える。次にビットレートだけ1500 kbit/sへ戻し、同じA/Bを繰り返す。低遅延コマンドの比較は次節で受信条件を分離して行う。

エラーを数える試験では、どちらのコマンドにも`-loglevel repeat+warning -nostats`を追加し、例えば末尾へ`2> A-500-1.log`を付ける。通常の重複ログ抑制で回数が見えなくなることを避ける。各試験名を方式・ビットレート・試行番号で揃える。PCのPowerShellでの集計例:

```powershell
@(Select-String -Path A-500-1.log -Pattern 'Packet corrupt').Count
@(Select-String -Path A-500-1.log -Pattern 'error while decoding MB|corrupted macroblock').Count
```

これはログ行数であり、UDP欠落数や破損フレーム数ではない。別の復号エラーも原文を保存する。開始直後の途中参加によるエラーと継続的なエラーを区別し、毎回同じ集計期間を使う。

| 条件（各3回） | Packet corrupt行数 | H.264エラー行数 | ブロックノイズ | 遅延中央値／最大ms | sender dropped | 試験中のPID変化 |
| --- | --- | --- | --- | --- | --- | --- |
| A multicast / 500 | 未測定 | 未測定 | 未確認 | 未測定 | 未測定 | 未確認 |
| B unicast / 500 | 未測定 | 未測定 | 未確認 | 未測定 | 未測定 | 未確認 |
| A multicast / 1500 | 未測定 | 未測定 | 未確認 | 未測定 | 未測定 | 未確認 |
| B unicast / 1500 | 未測定 | 未測定 | 未確認 | 未測定 | 未測定 | 未確認 |

unicastで破損が大幅に減れば、Wi-Fiのmulticast経路が主要因であることを強く示す。同程度なら送信タイミング・受信負荷・MPEG-TSをさらに調べる。senderの`dropped=0`はローカル送信APIで破棄がなかったという意味で、無線区間やPCの受信側で欠落がないことを保証しない。

### 受信バッファと遅延の段階比較

同じunicast送信と同じビットレートで、以下を1つずつ実行する。各試験は10秒のウォームアップ後に5分間行い、映像品質も同時に記録する。Test 1～4は`probesize=4096`と`analyzeduration=0`を共通にして追加オプションの効果を比較する。通常受信の既定解析量との差は別枠のTest 0として記録する。以前使用したコマンドも比較する場合は実際の引数をそのまま記録し、複数条件の変更を単一オプションの効果と混同しない。

| 試験 | Test 1に追加するオプション | 遅延中央値／最大ms | 破損・目視品質 |
| --- | --- | --- | --- |
| Test 0: 通常受信 | 解析量も含め既定値 | 未測定 | 未確認 |
| Test 1: 比較基準 | なし | 未測定 | 未確認 |
| Test 2 | `-max_delay 0` | 未測定 | 未確認 |
| Test 3 | 上記＋`-avioflags direct` | 未測定 | 未確認 |
| Test 4 | 上記＋`-fflags nobuffer -flags low_delay -framedrop` | 未測定 | 未確認 |

```sh
ffplay "udp://0.0.0.0:12346"
ffplay -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
ffplay -max_delay 0 -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
ffplay -max_delay 0 -avioflags direct -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
ffplay -max_delay 0 -avioflags direct -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
```

comma画面とPC画面を同時にスマートフォンの60fps以上の動画へ撮影する。停車状態で設定画面を開閉するなど、同じUI変化が本体とPCに現れたフレーム番号を読み取り、`(PC側フレーム番号 − 本体側フレーム番号) / 撮影fps × 1000`でmsへ換算する。60fpsで18フレーム差なら300ms。可変フレームレートの録画ではフレーム番号の代わりに動画の時刻を使う。

各条件で10個以上の変化を測り、中央値・最大値・標本数・撮影fpsを記録する。60fpsの1フレームは約16.7msで、表示更新と読み取りにも誤差があるため、数ms単位の差を断定しない。PC時計とcomma時計の同期は不要。起動して最初の映像が出るまでの時間は定常時のglass-to-glass遅延と分ける。

### 形式確認と受信できない場合

映像形式を調べる場合は以下を使用する。`<PC_IP>`を実際のPCのIPv4へ置換する。

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,profile,width,height,pix_fmt,has_b_frames,r_frame_rate \
  'udp://239.255.42.99:12346?localaddr=<PC_IP>'
```

FFplayとFFprobeは必要に応じて一方ずつ実行する。受信できない場合は、両端のIPv4、APの端末間隔離、IGMP・マルチキャスト制限、受信側のファイアウォールとインターフェースを確認する。パケットが届いているのに黒画面ならFFmpegのH.264デコードと入力解析時間を確認し、まず`-analyzeduration 0`や`-fflags nobuffer`を外して切り分ける。

MIB側ではUDPとH.264/MPEG-TSを有効にしたFFmpegビルドを持つstream-playerが必要。候補コマンドは`./stream-player udp://239.255.42.99:12346`。このリポジトリではMIBのビルド・転送・MOST設定は変更しない。

## 自動テスト

Linuxの本家開発環境では以下を実行する。

```sh
python -m unittest \
  openpilot.system.ui.lib.tests.test_screen_stream \
  openpilot.system.ui.lib.tests.test_screen_capture \
  openpilot.system.ui.lib.tests.test_screen_stream_settings -v

SCREEN_STREAM_TEST_GPU=1 python -m unittest \
  openpilot.system.ui.lib.tests.test_screen_stream_video -v
```

GPU統合テストはOpenGLコンテキストとFFmpeg、ループバック上のUDP multicast/unicastを必要とする。unicastの127.0.0.1は送信クラスを直接検証するテスト用オブジェクトだけに使い、本番設定の検証は引き続き拒否する。通常はスキップされる。FFmpegのパスを個別に指定する場合は`SCREEN_STREAM_TEST_FFMPEG`を使用する。エンコーダには`-protocol_whitelist file,pipe`を指定し、FFmpegのUDP対応に依存しないことを確認する。GPU→FFmpeg stdout→本番のPython送信クラス→ループバック受信→復号の経路で、合成した赤青の画像だけをPC内で送受信し、本物のUIやカメラ映像は使わない。

2026-09-22の開発PCでの結果:

- Python 3.12、Windows、Raylib 6.1-dev、FFmpeg 7.1で単体テスト65件とGPU統合テスト3件が成功。
- D-Bus照会失敗時の既存プロセス維持、100回連続失敗での警告集約、起動前の待機、正常な未接続・tuple変更、設定確認の独立性、不正設定のままWi-Fi検出に成功しても起動しないこと、各D-Busリクエストの独立した期限を検証。
- 不規則なstdout読み取り境界からの1316 bytes集約、正常EOFの端数送信、不完全なTS端数破棄、socket設定、致命的障害の通知、送信スレッド終了を確認。
- EAGAIN・ENOBUFS・EINTRなどの注入で送信継続とFFmpegの維持、破棄数・成功数・送信bytesを確認。100回の破棄で警告が100回出ないことを検証。
- フレーム鮮度と書き込み期限の分離、古いフレームだけの破棄、実際のパイプ停止による再起動、原因別cloudlogメッセージと再起動回数を検証。
- 無効時の無通信、最新フレームへの置き換え、Wi-Fi切断・再接続・IP変更、D-Bus障害、FFmpeg欠落・終了・書き込み停止からの復旧を確認。
- GPU縮小・上下方向・黒帯、H.264 Constrained Baseline / 800×480 / 20fps / yuv420p / Bフレームなし、188 bytes単位のMPEG-TS、最大1316 bytesの実UDP multicast/unicast送受信と復号を確認。
- Ruffによる変更Pythonファイルの検査を実施。
- 既定値に加え、別multicastアドレス・ポート・2600 kbit/s・TTL 2と、unicastループバック・500 kbit/sでもGPU→FFmpeg stdout→Python socket→UDP→復号を確認。
- 両方式のsocket設定・sendto宛先・1316 bytes集約・一時／致命的エラー、unicastのbind失敗時解放、TTL無視、方式切替両方向での後片付けと再生成を検証。
- 設定の境界値・不正値・URL文字列拒否・保存とキャンセル・録画中の編集無効化・OFF時の非表示・送信先変更時の再起動を確認。
- 本家のPOローダーで12言語の翻訳収録を確認し、英語から日本語への切替と表示更新を検証。

実機ではPython socket経由のPC受信と`9f0d5360e`でのPID安定・local drop 0件が報告済み。今回のunicastと受信オプションによる破損・遅延の改善は未測定。開発PCの結果はcomma 3Xでの本体ビルド・本体UI全体・本家CI全体の成功を意味しない。Windowsテストではswaglogのネイティブ依存をモックへ差し替え、cloudlogへ渡すメッセージを検証する。実機で既存cloudlogが保存されることは報告済み。

## 実機テスト記録

| 確認項目 | 手順 | 結果 |
| --- | --- | --- |
| ビルドと設定 | 新規キー認識、初期値OFF、ON/OFF反映、再起動後の保存 | 未実施 |
| 設定編集と翻訳 | ON時のみ4項目を表示、変更した宛先で受信、言語変更でラベルを更新 | 未実施 |
| 通常表示 | 配信OFF/ONの双方で本体UI・設定画面が正常に表示 | 未実施 |
| 録画との排他 | 配信ONの設定を残して`RECORD=1`でUI起動、MP4のみ生成 | 未実施 |
| 負荷 | OFF/ONを各10分、UI FPS、CPU、温度、メモリを比較 | 未実施 |
| 遅延 | 本体と受信画面を同じ動画に撮り、変化の時間差を測定 | 未実施 |
| 通信断 | AP切断、再接続、DHCP更新、受信側再起動 | 未実施 |
| 複数回線 | 携帯回線/Ethernet併用時もWi-Fiだけから送出 | 未実施 |
| 消灯と復帰 | 本体消灯で配信停止、点灯で自動再開 | 未実施 |
| 長時間動作 | 繰り返しON/OFF後にFFmpegやGPUリソースが残らない | 未実施 |
| 車両への影響 | 制御周期や警告、UI応答を配信OFF時と比較 | 未実施 |
| MIB/AID | PC受信確認後、MIB→MOST→AIDの表示を確認 | 未実施 |

CPU負荷・遅延が許容できない場合はビットレート、解像度、FPSを調整する。エンコードはソフトウェアのlibx264を使用する。本家へPRする前に上の結果を埋め、コード内容を確認し、必要に応じてキャプチャ整理・バックエンド・設定UIの単位に分ける。

## 参考

- [FFmpeg UDPプロトコル](https://ffmpeg.org/ffmpeg-protocols.html#udp)
- [FFmpeg MPEG-TS](https://ffmpeg.org/ffmpeg-formats.html#mpegts)
- [本家の開発ガイド](CONTRIBUTING.md)
- [AI支援に関する本家方針](AI_POLICY.md)
