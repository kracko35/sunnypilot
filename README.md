# sunnypilot

[sunnypilot](https://github.com/sunnypilot/sunnypilot) のUIを、Wi-Fi上のUDPマルチキャストで配信する開発用forkです。録画経路のRenderTextureを共有し、H.264映像をMPEG-TSとして送信します。MIB / MOST / AID側の変更は含みません。

A development fork of [sunnypilot](https://github.com/sunnypilot/sunnypilot) that streams its UI over Wi-Fi using UDP multicast, H.264 and MPEG-TS. It shares the recorder's RenderTexture capture path. MIB / MOST / AID receiver changes are outside this repository.

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
5. PCで次を実行します（既定の宛先の場合）。 / On the receiver PC, run (for the default destination):

```sh
ffplay -fflags nobuffer -flags low_delay -framedrop \
  -probesize 32768 -analyzeduration 0 \
  'udp://239.255.42.99:12346?fifo_size=256&overrun_nonfatal=1'
```

複数のネットワークがあるPCでは、受信URLへ`&localaddr=<PCのWi-Fi IPv4>`を追加してください。APの端末間隔離やマルチキャスト制限があると受信できません。

On a PC with multiple network interfaces, append `&localaddr=<PC Wi-Fi IPv4>` to the receiver URL. AP client isolation or multicast filtering may prevent reception.

送信先を変更した場合は受信URLも同じアドレス・ポートへ変更してください。設定は再起動後も保持されます。UIの英語原文をsunnypilot標準の`tr`／`tr_noop`とPOファイルで翻訳し、日本語を含む既存の12言語に対応しています。

If you change the destination, update the receiver URL to match its address and port. Settings persist across restarts. English UI source strings use sunnypilot's standard `tr` / `tr_noop` and PO catalogs, with translations for all 12 supported languages.

| 設定 / Setting | 許容値 / Allowed values | 既定値 / Default |
| --- | --- | --- |
| Multicast Address | IPv4: 224.0.1.0–239.255.255.255 | 239.255.42.99 |
| UDP Port | 1–65535 | 12346 |
| Bitrate (kbit/s) | 250–8000 | 1500 |
| Multicast TTL | 1–255 | 1 |

TTLは同一ネットワーク内なら1を使用します。無効な入力は保存しません。マルチキャスト専用のため、ユニキャストIP・ホスト名・URLは受け付けません。配信設定キーを使うには更新後の再ビルドが必要です。

Use TTL 1 for the local network. Invalid input is rejected. Only multicast IPv4 destinations are accepted; unicast IPs, hostnames and URLs are not supported. Rebuild after updating to register the new parameter keys.

## 配信仕様 / Stream settings

| 項目 / Item | 値 / Value |
| --- | --- |
| 既定の宛先 / Default destination | `239.255.42.99:12346` |
| 映像 / Video | 800×480、最大20fps / up to 20 fps |
| 縦横比 / Aspect ratio | 維持・余白は黒 / preserved with black bars |
| エンコーダ / Encoder | libx264, baseline, yuv420p, veryfast, zerolatency |
| ビットレート / Bitrate | 設定可能、既定1500 kbit/s。VBVは約1/3 / configurable, default 1500 kbit/s; VBV about one third |
| GOP / Bフレーム / B-frames | 10 / 0 |
| 形式 / Format | H.264 / MPEG-TS / UDP multicast |
| TTL / UDP payload | TTL設定可能（既定1）/ 最大1316 bytes / configurable TTL (default 1), up to 1316 bytes |
| フレーム待ち行列 / Pending frames | 最新1枚 / one latest frame |
| 永続設定 / Persistent parameter | `ScreenStreamEnabled`、初期値OFF / default off |

録画`RECORD=1`が優先され、同時配信しません。Wi-Fi未接続時・画面消灯時は停止し、接続・画面描画の再開時に自動復帰します。Wi-FiのIPv4に送信を束縛し、携帯回線・Ethernet・本体のAPモードでは配信しません。音声と描画後のデバッグ表示は含みません。

`RECORD=1` takes precedence and disables streaming. Streaming stops while Wi-Fi is disconnected or the display is asleep and resumes automatically. Output is bound to the connected Wi-Fi IPv4; cellular, Ethernet and device hotspot mode are excluded. Audio and post-render debug overlays are not included.

配信は暗号化・認証されません。有効化すると、同じWi-Fiの受信端末へ設定画面を含むUIが公開されます。OFFにすると配信とキャプチャを停止します。

The stream is unencrypted and unauthenticated. Enabling it exposes the UI, including settings screens, to receivers on the same Wi-Fi. Disabling it stops streaming and capture.

## 検証と開発 / Validation and development

Windows開発PCで単体テストとGPU・FFmpeg・ループバックUDPマルチキャストの統合テストを実行済みです。comma 3X上のビルド、負荷・温度・実遅延、実Wi-FiとMIB / MOST / AID表示は未検証です。

Unit tests and GPU / FFmpeg / loopback UDP multicast integration tests have been run on a Windows development PC. Device builds, comma 3X load, temperature, end-to-end latency, real Wi-Fi, and MIB / MOST / AID display remain unverified.

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
