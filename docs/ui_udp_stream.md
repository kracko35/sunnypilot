# UDP画面配信

## 目的と対象

comma 3Xのsunnypilot UIを同一Wi-Fiの受信端末へ配信する。送信側は汎用的な画面配信機能とし、MIB・MOST・AIDへの依存を持たせない。受信側の候補はFFplayとVcMOSTRenderMqbのstream-player。実機への導入やMIB側の操作は、この変更では実行していない。

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

- `system/ui/lib/screen_stream.py`: 接続Wi-Fiの取得、FFmpegプロセス管理、最新フレームの待ち行列、書き込み期限、再試行。`MpegTsMulticastSender`が標準出力の読み取り・TS分割・Python socketでの送信を担当する。
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
| Multicast Address | `ScreenStreamAddress` | 239.255.42.99 | IPv4の224.0.1.0～239.255.255.255 |
| UDP Port | `ScreenStreamPort` | 12346 | 1～65535 |
| Bitrate (kbit/s) | `ScreenStreamBitrate` | 1500 | 250～8000 |
| Multicast TTL | `ScreenStreamTtl` | 1 | 1～255 |

アドレスはマルチキャスト専用で、ユニキャストIP・ホスト名・URL・224.0.0.0/24の制御用アドレスは受け付けない。TTLは同一ネットワーク内では1を使用する。設定は再起動後も保持し、OFFにしても値を消去しない。解像度800×480と最大20fpsは固定。

`screen_stream_config.py`の検証をUI入力と配信バックエンドで共用する。無効な入力は保存せず、翻訳されたエラーと入力範囲を表示して再入力を求める。キャンセル時は値を変更しない。設定は通常1秒周期のワーカー側チェックで反映し、変更時に古いFFmpeg・送信スレッド・socket・待機フレームを解放し、エンコーダと送信処理を再生成する。本体UIの再起動は不要。新しいキーを登録するため、この更新を初めて導入するときはネイティブライブラリの再ビルドが必要。

共通UIは`system/ui/widgets/screen_stream_settings.py`に置き、comma 3Xでは標準のリストとKeyboard、小画面UIではBigButtonとBigInputDialogを使用する。翻訳の原文は同ファイルの`tr_noop`で抽出でき、テンプレートは`selfdrive/ui/translations/app.pot`、翻訳は`app_*.po`へ格納する。配信設定の文字列は12言語すべてに翻訳を収録する。以後の更新は本家の`python -m openpilot.selfdrive.ui.translations.update_translations`を使用できる。

### キャプチャ

```text
通常UIのRenderTexture
  ├─ 本体画面
  └─ GPUで800×480へ縮小（縦横比保持・黒帯）
       └─ RGBA読み出し → 最新1フレーム → FFmpeg stdin
            → libx264 / MPEG-TS → stdout (pipe:1)
            → 専用送信スレッド → Python socket → UDPマルチキャスト
```

2160×1080のフル画面をCPUへ読み出す代わりに800×480で読み出す。20fps時の生RGBA転送量は約30.72 MB/sで、フル解像度の約186.62 MB/sより小さい。これは画素数からの計算で、実測値ではない。

OpenGLの上下方向は縮小描画時とFFmpegの`vflip`で揃える。FPS表示・タッチ位置など、画面転送後のデバッグ表示は配信に含まない。元のUIが低フレームレートのときに不足フレームを再生したり複製したりしない。

### エンコードと送信

固定設定は800×480、最大20fps、libx264 baseline / yuv420p、Bフレームなし、GOP 10、`veryfast`、`zerolatency`。ビットレートは設定値を使い、VBVはその約1/3とする。SPS/PPSを繰り返し、MPEG-TSで送信する。既定値は1500 kbit/s、VBV 500 kbit、宛先`239.255.42.99:12346`、TTL 1。UDPペイロードはPython側で最大1316 bytesに制限する。FFmpeg URLの`pkt_size`オプションは使用しない。

Wi-Fiの判定は、NetworkManagerの接続済みWi-Fiデバイス、インフラストラクチャモード、有効なIPv4の組み合わせで行う。デフォルトルートが携帯回線でもWi-Fiが接続済みなら配信できる。Pythonの`socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)`を使い、`IP_MULTICAST_IF`へWi-FiのIPv4を、`IP_MULTICAST_TTL`へ設定値を指定する。`bind`は使用しない。Wi-Fiインターフェース名・IPv4・宛先・ポート・ビットレート・TTLのいずれかが変われば、FFmpegとsocketを作り直す。

UIスレッドはFFmpegへ直接書かない。ワーカーが非ブロッキングパイプを使い、取得から250ms以内にフレームを書き切れなければプロセスを再起動する。書きかけのrawvideoを途中で捨てて次フレームへ継ぎ足すことはしない。ワーカーのスケジューラとniceを変更した後にFFmpegと送信スレッドを生成し、UIのリアルタイム優先度を継承させない。エンコーダのスレッド数も2に制限する。

Wi-Fiを通常1秒周期で確認する。OFF・消灯は通常100ms以内に確認し、処理中の照会・書き込み・子プロセス終了処理分の遅延が加わる。障害後は約3秒待って再試行する。これらは処理の期限であり、端末間の映像遅延を保証する値ではない。

OFF時はWi-Fi照会・FFmpeg・socketを動作させず、配信専用のGPUリソースを解放する。表示用・録画用に必要なRenderTextureは維持する。

### FFmpegのパイプ出力と送信スレッド

実機から報告されたcomma 3Xの標準FFmpegは、入力・出力ともに`file`と`pipe`のみを提供する。`libx264`と`mpegts`は利用できるが、FFmpegの`udp`プロトコルは利用できない。このためエンコードと送信を分離する。

```text
FFmpeg出力の変更前: udp://ADDRESS:PORT?pkt_size=1316&ttl=TTL&localaddr=LOCALADDR
FFmpeg出力の変更後: pipe:1
```

`ffmpeg_command(config)`は送信元アドレスを引数に取らず、宛先・ポート・TTLもコマンドへ渡さない。エンコード条件は維持する。標準入力・標準出力を`subprocess.PIPE`、`bufsize=0`で開き、stderrは従来どおり継承する。

ワーカーはRGBAをstdinへ書き、別の送信スレッドがstdoutを常時読み出す。読み書きを同じスレッドで処理しないため、stdoutの満杯でエンコーダが停止し、stdin書き込みまで停止する循環待ちを防ぐ。stdoutは非ブロッキング読み取り、socketは100msの送信タイムアウトを使用する。

読み取り境界とTSパケット境界は一致しない。読み取ったデータを一時バッファへ追記し、188 bytesの整数倍・最大1316 bytesずつ順序どおり`sendto()`する。187 bytes以下の端数を次の読み取りへ保持し、EOF時の不完全なパケットは送信しない。通常の完全なTSストリームに欠落・重複・並べ替えを加えない。

送信スレッドの例外は保持してワーカーへ通知し、元の例外を原因としてログへ出す。stdoutのEOFも停止として検出する。障害時はreadyを解除して送信停止を通知し、FFmpegをterminate、200msで終了しなければkillしてさらに200ms待つ。stdin/stdoutを閉じ、送信スレッドを最大500msでjoinし、socketとフレーム待ち行列を解放する。約3秒後に再試行する。消灯やOFFでも同じ後片付けを行い、点灯・ON時は自動再開する。

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
pgrep -af ffmpeg
```

FFmpegの出力先が`pipe:1`であり、プロセスがすぐに終了しないことを確認する。受信PCではREADMEのFFplayコマンドで映像を確認する。受信側FFplayのUDP機能は引き続き必要。OFFや消灯でFFmpegが終了し、再開時に復帰することも確認する。

設定画面から「UDP Screen Streaming」（日本語では「UDP画面配信」）をONにする。SSHから設定する場合は次の操作が同等。ONにすると設定画面を含め同じLANへ画面が公開される。

```sh
python -c 'from openpilot.common.params import Params; Params().put_bool("ScreenStreamEnabled", True, block=True)'
```

OFFへ戻すには`True`を`False`へ変更する。ブランチのロールバックは、記録した元のブランチへ切り替え、サブモジュールを同期して再起動する。

送信先アドレス・ポートを変更した場合は、以下の受信URLにも同じ値を指定する。

## 受信と切り分け

PCでの最初の受信はREADMEのFFplayコマンドを使う。映像形式を調べる場合は以下を使用する。

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,profile,width,height,pix_fmt,has_b_frames,r_frame_rate \
  'udp://239.255.42.99:12346?fifo_size=256&overrun_nonfatal=1'
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

GPU統合テストはOpenGLコンテキストとFFmpeg、ループバック上のUDPマルチキャストを必要とする。通常はスキップされる。FFmpegのパスを個別に指定する場合は`SCREEN_STREAM_TEST_FFMPEG`を使用する。エンコーダには`-protocol_whitelist file,pipe`を指定し、FFmpegのUDP対応に依存しないことを確認する。GPU→FFmpeg stdout→本番のPython送信クラス→ループバック受信→復号の経路で、合成した赤青の画像だけをPC内で送受信し、本物のUIやカメラ映像は使わない。

2026-09-22の開発PCでの結果:

- Python 3.12、Windows、Raylib 6.1-dev、FFmpeg 7.1で単体テスト44件とGPU統合テスト2件が成功。
- 不規則なstdout読み取り境界からのTS再構成、EOF端数破棄、socket設定、送信・読み取り障害の通知、送信スレッド終了を確認。
- 無効時の無通信、最新フレームへの置き換え、Wi-Fi切断・再接続・IP変更、D-Bus障害、FFmpeg欠落・終了・書き込み停止からの復旧を確認。
- GPU縮小・上下方向・黒帯、H.264 Constrained Baseline / 800×480 / 20fps / yuv420p / Bフレームなし、188 bytes単位のMPEG-TS、最大1316 bytesの実UDPマルチキャスト送受信と復号を確認。
- Ruffによる変更Pythonファイルの検査を実施。
- 既定値に加え、別のアドレス・ポート・2600 kbit/s・TTL 2でもGPU→FFmpeg stdout→Python socket→UDP→復号を確認。
- 設定の境界値・不正値・URL文字列拒否・保存とキャンセル・録画中の編集無効化・OFF時の非表示・送信先変更時の再起動を確認。
- 本家のPOローダーで12言語の翻訳収録を確認し、英語から日本語への切替と表示更新を検証。

実機からはParamsの保存値とWi-Fi検出（wlan0）が正常で、標準FFmpegのUDP非対応によりエンコーダが即終了することが報告されている。本修正後のcomma 3X上での配信動作は未確認。開発PCの結果はcomma 3Xでの実測・本体ビルド・本家CI全体の成功を意味しない。Windowsには本家のLinuxネイティブ依存が揃わないため、本体UI全体の起動テストは未実施。

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
