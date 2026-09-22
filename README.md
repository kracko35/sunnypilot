# sunnypilot

[sunnypilot](https://github.com/sunnypilot/sunnypilot) のUIを、Wi-Fi上のUDPユニキャスト／マルチキャストで配信する開発用forkです。録画経路のRenderTextureを共有し、FFmpegのMPEG-TS標準出力をPythonのUDP socketで送信します。MIB / MOST / AID側の変更は含みません。

A development fork of [sunnypilot](https://github.com/sunnypilot/sunnypilot) that streams its UI over Wi-Fi using UDP unicast or multicast, H.264 and MPEG-TS. It shares the recorder's RenderTexture capture path and sends FFmpeg's MPEG-TS stdout through a Python UDP socket. MIB / MOST / AID receiver changes are outside this repository.

## ブランチ / Branches

```text
upstream/master
  └─ udp-screen-streaming
```

`udp-screen-streaming`はmaster起点の開発・将来のPR用ブランチです。実機テスト用ブランチと変更の反映先は未定です。

`udp-screen-streaming` is the development branch based on master for future upstream PRs. The device-test branch and integration target have not been selected.

## 使い方 / Usage

1. 配信設定キーを含むソースのビルドを完了します。実機テスト用ブランチは未定です。 / Build the source with the streaming parameter keys. A device-test branch has not been selected.
2. commaと受信端末を同じWi-Fiへ接続します。 / Connect comma and the receiver to the same Wi-Fi network.
3. 設定の「トグル」→「UDP画面配信」をONにします。初期値はOFF、再起動不要です。英語UIでは “UDP Screen Streaming” と表示します。 / Enable “UDP Screen Streaming” in Settings → Toggles. It is off by default and takes effect without restarting.
4. ONにするとアドレス・ポート・ビットレート・TTLの設定欄が表示されます。変更は保存後に自動反映されます。 / Enabling streaming reveals address, port, bitrate and TTL settings. Saved changes apply automatically.
5. 送信先を選び、PCで以下の受信コマンドを実行します。 / Select the destination and run a receiver command below on the PC.

通常受信: multicastは既定の送信先を使用し、`<PC_IP>`をPCのWi-Fi IPv4へ置換します。unicastは設定の送信先をPCのIPv4（例: `192.168.4.44`）へ変更します。コマンドはどちらか一方だけ実行してください。

Normal reception: for multicast, keep the default destination and replace `<PC_IP>` with the PC's Wi-Fi IPv4. For unicast, set the destination to the PC's IPv4 (e.g. `192.168.4.44`). Run only one receiver at a time.

```text
multicast:
ffplay "udp://239.255.42.99:12346?localaddr=<PC_IP>"

unicast:
ffplay "udp://0.0.0.0:12346"
```

低遅延テスト: 同じ送信設定で以下と比較します。受信FIFOはFFmpegの既定値を使います。

Low-latency test: compare the following with the same sender settings. Leave the receive FIFO at FFmpeg's default.

```text
multicast:
ffplay -max_delay 0 -avioflags direct -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://239.255.42.99:12346?localaddr=<PC_IP>"

unicast:
ffplay -max_delay 0 -avioflags direct -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
```

Windowsなどで特定インターフェースへ受信を束縛する場合は、unicast URLを`udp://0.0.0.0:12346?localaddr=192.168.4.44`へ変更します。APの端末間隔離やマルチキャスト制限があると受信できません。ポート変更時は受信URLのポートも変更します。

To bind reception to a specific interface, including on Windows, use the unicast URL `udp://0.0.0.0:12346?localaddr=192.168.4.44`. AP client isolation or multicast filtering may prevent reception. Match the receiver port to the configured destination port.

低遅延オプションの効果は実測が必要です。[方式比較・遅延測定手順](docs/ui_udp_stream.md#受信と切り分け)に、500/1500 kbit/sの比較と受信オプションを段階的に追加する手順を記載しています。設定は再起動後も保持されます。英語UI原文はsunnypilot標準の`tr`／`tr_noop`とPOで翻訳し、日本語を含む12言語に対応します。

Measure the actual effect of low-latency options. The [transport and latency comparison guide (Japanese)](docs/ui_udp_stream.md#受信と切り分け) covers 500/1500 kbit/s trials and incremental receiver options. Settings persist across restarts. English UI source strings use sunnypilot's standard `tr` / `tr_noop` and PO catalogs, with translations for all 12 supported languages.

| 設定 / Setting | 許容値 / Allowed values | 既定値 / Default |
| --- | --- | --- |
| Destination Address / 送信先アドレス | 通常のIPv4 unicast、または / ordinary IPv4 unicast, or multicast 224.0.1.0–239.255.255.255 | 239.255.42.99 |
| UDP Port | 1–65535 | 12346 |
| Bitrate (kbit/s) | 250–8000 | 1500 |
| Multicast TTL | 1–255 | 1 |

TTLはmulticast専用で、同一ネットワークでは1を使用します。unicastでは保存値を送信に使いません。無効な入力は保存しません。0.0.0.0、0/8、loopback、link-local、予約済みIPv4、限定ブロードキャスト、224.0.0/24、IPv6、ホスト名、URL、ポートやクエリ付きアドレスは拒否します。既存の`ScreenStreamAddress`キーと既定値は維持します。配信設定キーのないビルドから導入する場合は再ビルドが必要です。

TTL applies only to multicast; use 1 for the local network. Unicast ignores the saved TTL. Invalid input is rejected, including unspecified, 0/8, loopback, link-local, reserved IPv4, limited broadcast, 224.0.0/24, IPv6, hostnames, URLs, and addresses with ports or query strings. The existing `ScreenStreamAddress` key and default remain unchanged. Rebuild when upgrading from a build without the streaming parameter keys.

## 送信経路 / Transport

```text
UI RGBA → FFmpeg stdin → libx264 / MPEG-TS → stdout (pipe:1)
                                               ↓
                              Python socket → UDP unicast / multicast
```

comma 3Xで確認された標準FFmpegは`file`と`pipe`のみをサポートするため、FFmpegのUDPプロトコルには依存しません。必要なのは`libx264`、`mpegts`、`pipe`です。専用スレッドのPython socketで送信します。multicastはWi-Fi IPv4とTTLを設定し、unicastはWi-Fi IPv4へbindしてTTL設定を無視します。Python側で通常のUDPペイロードを1316 bytesへ集約し、正常終了時の最後だけ188 bytesの整数倍で短く送信できます。一時的な送信バッファ不足ではそのデータグラムだけを破棄し、FFmpegは維持します。

The standard FFmpeg build reported on comma 3X supports only `file` and `pipe`, so streaming does not depend on FFmpeg's UDP protocol. It requires `libx264`, `mpegts`, and `pipe`. A dedicated Python socket thread sends the data. Multicast sets the Wi-Fi IPv4 interface and TTL; unicast binds to the Wi-Fi IPv4 and ignores the multicast TTL setting. Python coalesces normal UDP payloads to 1316 bytes; only the final payload at clean EOF may be shorter, in multiples of 188 bytes. Temporary send-buffer pressure drops the affected datagram while keeping FFmpeg running.

フレーム取得から250msを超えた未送信フレームは破棄します。パイプの書き込み期限は書き込み開始から500msです。再起動理由・PID・UDP破棄数は標準のcloudlogへ記録します。[実機診断手順](docs/ui_udp_stream.md#実機での再起動診断)を参照してください。

Queued frames older than 250ms are discarded. Pipe writes have a separate 500ms deadline measured from the start of writing. Restart reasons, PIDs and UDP drop counts are recorded through the standard cloudlog logger. See the [device diagnostics](docs/ui_udp_stream.md#実機での再起動診断).

実機ログでは、複数のNetworkManager D-Bus照会が共有していた250msの期限超過を、配信障害として扱うことが周期的再起動の主因でした。各照会に独立した250msの期限を与え、照会失敗時は最後に確認できたWi-Fi情報で配信を続けます。正常な照会結果が未接続（`None`）なら停止し、接続先が変われば送信処理を再生成します。ネットワーク確認は接続中5秒・未接続時1秒、設定確認は独立した1秒周期です。

Device logs identified a shared 250ms deadline across multiple NetworkManager D-Bus requests, with query timeouts treated as streaming failures, as the main cause of periodic restarts. Each request now receives its own 250ms timeout. Failed queries preserve the last known Wi-Fi connection and keep streaming; successful queries returning no connection (`None`) stop streaming, and connection changes rebuild the transport. Network checks run every 5 seconds while connected and every second while disconnected. Settings are checked independently every second.

## 配信仕様 / Stream settings

| 項目 / Item | 値 / Value |
| --- | --- |
| 既定の宛先 / Default destination | `239.255.42.99:12346` |
| 映像 / Video | 800×480、最大20fps / up to 20 fps |
| 縦横比 / Aspect ratio | 維持・余白は黒 / preserved with black bars |
| エンコーダ / Encoder | libx264, baseline, yuv420p, veryfast, zerolatency |
| ビットレート / Bitrate | 設定可能、既定1500 kbit/s。VBVは約1/3 / configurable, default 1500 kbit/s; VBV about one third |
| GOP / Bフレーム / B-frames | 10 / 0 |
| 形式 / Format | H.264 / MPEG-TS / UDP unicast or multicast |
| TTL / UDP payload | multicast TTL設定可能（既定1）/ 通常1316 bytes / configurable multicast TTL (default 1), normally 1316 bytes |
| フレーム待ち行列 / Pending frames | 最新1枚 / one latest frame |
| 永続設定 / Persistent parameter | `ScreenStreamEnabled`、初期値OFF / default off |

録画`RECORD=1`が優先され、同時配信しません。Wi-Fi未接続時・画面消灯時は停止し、接続・画面描画の再開時に自動復帰します。Wi-FiのIPv4に送信を束縛し、携帯回線・Ethernet・本体のAPモードでは配信しません。音声と描画後のデバッグ表示は含みません。

`RECORD=1` takes precedence and disables streaming. Streaming stops while Wi-Fi is disconnected or the display is asleep and resumes automatically. Output is bound to the connected Wi-Fi IPv4; cellular, Ethernet and device hotspot mode are excluded. Audio and post-render debug overlays are not included.

配信は暗号化・認証されません。有効化すると、同じWi-Fiの受信端末へ設定画面を含むUIが公開されます。OFFにすると配信とキャプチャを停止します。

The stream is unencrypted and unauthenticated. Enabling it exposes the UI, including settings screens, to receivers on the same Wi-Fi. Disabling it stops streaming and capture.

## 検証と開発 / Validation and development

Windows開発PCで単体テスト65件とGPU・FFmpeg標準出力・Python socket・ループバックUDPの統合テスト3件（multicast 2件、unicast 1件）が成功しました。統合テストのエンコーダには`file,pipe`だけを許可しています。comma 3Xでは`9f0d5360e`でPID安定、送信21536件・local drop 0件の報告があります。一方、multicast受信の破損と約1000msの遅延が残っています。今回のunicast対応と受信オプションによる実機の改善、負荷・温度、MIB / MOST / AID表示は未検証です。

On a Windows development PC, 65 unit tests and 3 GPU / FFmpeg stdout / Python socket / loopback UDP integration tests passed (2 multicast, 1 unicast). The test encoder permits only `file,pipe`. Device reports for `9f0d5360e` confirm a stable PID and 21,536 sends with zero local drops, but multicast corruption and roughly 1,000 ms latency remain. Device improvements from unicast and receiver options, load, temperature, and MIB / MOST / AID display remain unverified.

[実装仕様・ビルド・実機テスト手順 / Implementation, build and device-test guide (Japanese)](docs/ui_udp_stream.md)

```sh
git clone --recurse-submodules --branch udp-screen-streaming \
  https://github.com/kracko35/sunnypilot.git
cd sunnypilot
git remote add upstream https://github.com/sunnypilot/sunnypilot.git
git fetch upstream
```

クローン後はリポジトリのディレクトリへ移動してからremoteを追加してください。本家へのPR先は`master`です。ブランチ更新と検証の詳細は上記手順書を参照してください。

After cloning, enter the repository directory before adding the remote. Future upstream PRs target `master`; see the guide for branch maintenance and validation.

## 本家・ライセンス / Upstream and license

このforkはsunnypilotとcomma.ai openpilotを基にしています。本家のドキュメントは[docs.sunnypilot.ai](https://docs.sunnypilot.ai/)、コミュニティは[community.sunnypilot.ai](https://community.sunnypilot.ai/)です。本家への支援は[GitHub Sponsors](https://github.com/sponsors/sunnyhaibin)から行えます。

This fork is based on sunnypilot and comma.ai openpilot. See the upstream [documentation](https://docs.sunnypilot.ai/), [community](https://community.sunnypilot.ai/), and [GitHub Sponsors](https://github.com/sponsors/sunnyhaibin).

本家と同様に、走行データの収集・アップロードに関する設定が適用されます。画面のUDP配信は別機能です。ライセンス、著作権表示、免責条項は[LICENSE](LICENSE)と[LICENSE.md](LICENSE.md)を参照してください。以下の原文を保持します。

Upstream driving-data collection and upload settings still apply. UDP screen streaming is separate. See [LICENSE](LICENSE) and [LICENSE.md](LICENSE.md) for copyright, license and disclaimer terms. The original notice is retained below:

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys’ fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**
