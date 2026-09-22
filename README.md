# sunnypilot

[sunnypilot](https://github.com/sunnypilot/sunnypilot) のUIをWi-Fi経由でH.264 / MPEG-TS / UDP配信する開発用forkです。現在は **ultra low latency experimental** 版として、unicastで端末間100ms以下を目標に計測・調整しています。100ms達成は未確認です。multicastと既存の設定互換性も維持します。

A development fork of [sunnypilot](https://github.com/sunnypilot/sunnypilot) that streams its UI over Wi-Fi using H.264 / MPEG-TS / UDP. This **ultra low latency experimental** version instruments and tunes unicast toward a sub-100 ms end-to-end target. That target has not been demonstrated on the device. Multicast and existing settings remain supported.

## ブランチ / Branch

```text
upstream/master
  └─ udp-screen-streaming
```

開発は`udp-screen-streaming`のみ。本家への将来のPR先はmasterで、実機テスト用ブランチとcherry-pick先は未定です。

Development stays on `udp-screen-streaming`. Future upstream PRs target master; the device-test branch and cherry-pick destination are undecided.

## 使い方 / Usage

1. 配信パラメーターを含むソースをビルドし、commaとPCを同じWi-Fiへ接続します。 / Build the source with streaming parameter keys and connect comma and the PC to the same Wi-Fi.
2. Settings → Toggles → “UDP Screen Streaming”（日本語: UDP画面配信）をONにします。既定はOFFです。 / Enable “UDP Screen Streaming” in Settings → Toggles. It is off by default.
3. “Destination Address”（送信先アドレス）にPCのWi-Fi IPv4（例: `192.168.4.44`）、ポートに`12346`を指定します。設定変更は自動反映されます。 / Set Destination Address to the PC's Wi-Fi IPv4 (e.g. `192.168.4.44`) and port to `12346`. Saved changes apply automatically.
4. 以下の受信コマンドを1つだけ起動します。まず500 kbit/sで試し、1500 kbit/sで統計と遅延を比較します。 / Start one receiver below. Test at 500 kbit/s first, then compare statistics and latency at 1500 kbit/s.

| 設定 / Setting | 許容値 / Allowed values | 既定値 / Default |
| --- | --- | --- |
| Destination Address / 送信先アドレス | 通常のIPv4 unicast、または / ordinary IPv4 unicast, or multicast 224.0.1.0–239.255.255.255 | 239.255.42.99 |
| UDP Port | 1–65535 | 12346 |
| Bitrate (kbit/s) | 250–8000 | 1500 |
| Multicast TTL | 1–255（unicastでは無視 / ignored for unicast） | 1 |

既存Param名と既定値は維持し、保存済みのunicast宛先もそのまま使用します。0/8、loopback、link-local、予約済みIPv4、限定broadcast、224.0.0/24、IPv6、ホスト名、URL、ポートやクエリ付き入力は拒否します。英語UI原文を本家の`tr`／`tr_noop`とPOで翻訳し、既存の12言語を使用します。

Existing parameter names, defaults, and saved unicast destinations are preserved. Validation rejects 0/8, loopback, link-local, reserved IPv4, limited broadcast, 224.0.0/24, IPv6, hostnames, URLs, and addresses containing ports or queries. English UI sources use upstream `tr` / `tr_noop` and PO catalogs for all 12 supported languages.

## PC受信 / PC reception

通常のunicast受信 / Normal unicast reception:

```sh
ffplay "udp://0.0.0.0:12346"
```

低遅延unicastテスト / Low-latency unicast test:

```sh
ffplay -max_delay 0 -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
```

multicastは本体の送信先を`239.255.42.99`へ戻し、`<PC_IP>`をPCのWi-Fi IPv4へ置換します。 / For multicast, set the sender destination to `239.255.42.99` and replace `<PC_IP>` with the PC's Wi-Fi IPv4:

```sh
ffplay -max_delay 0 -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://239.255.42.99:12346?localaddr=<PC_IP>"
```

unicastの特定インターフェース待受には`udp://0.0.0.0:12346?localaddr=192.168.4.44`を使用します。`0.0.0.0`は受信URL専用で、本体の送信先には指定しません。APの端末間隔離や受信側ファイアウォールも確認してください。

To bind unicast reception to a specific interface, use `udp://0.0.0.0:12346?localaddr=192.168.4.44`. Use `0.0.0.0` only in the receiver URL, never as the sender destination. Check AP client isolation and the receiver firewall.

`-avioflags direct`は使用しません。Windows FFplayの実機試験で “Part of datagram lost due to insufficient buffer size” と映像破損が報告されたため、推奨から削除しました。受信FIFOは既定値を使用します。

Do not use `-avioflags direct`: device testing with Windows FFplay reported “Part of datagram lost due to insufficient buffer size” and corrupted video. It has been removed from the recommended commands. Leave the receive FIFO at its default.

GStreamerを導入済みの場合の低遅延unicast候補（PowerShell用1行） / Low-latency unicast candidate for an installed GStreamer environment (one line for PowerShell):

```powershell
gst-launch-1.0 -v udpsrc address=0.0.0.0 port=12346 buffer-size=65536 caps="video/mpegts,systemstream=(boolean)true,packetsize=(int)188" "!" tsdemux latency=0 "!" h264parse "!" avdec_h264 "!" queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "!" videoconvert "!" autovideosink sync=false async=false
```

PowerShellで改行する場合は行末にバッククォートを使用し、cmd用の`^`は使いません。GStreamerコマンドの実機動作・遅延は未確認です。必要なプラグインと比較手順は[手順書](docs/ui_udp_stream.md#受信と切り分け)を参照してください。

For multiline PowerShell commands, use a trailing backtick, not cmd's `^`. Device operation and latency of this GStreamer command remain unverified. See the [guide (Japanese)](docs/ui_udp_stream.md#受信と切り分け) for required plugins and comparisons.

## 実験版の変更 / Experimental changes

| 項目 / Item | 設定 / Setting |
| --- | --- |
| 映像 / Video | 800×480、最大20fps、黒帯で縦横比維持 / up to 20 fps, aspect ratio preserved with black bars |
| Timestamp | FFmpeg入力のwall clock / FFmpeg input wall clock |
| Encoder | libx264 ultrafast, zerolatency, baseline, yuv420p, B=0, GOP=10, lookahead=0 |
| VBV | bitrate / 10（1500 kbit/sなら150 kbit）/ 150 kbit at 1500 kbit/s |
| 古い未送信frame / Stale pending frames | 75ms以上で破棄 / discard at 75 ms |
| stdin書き込み期限 / Write deadline | 書き込み開始から100ms / 100 ms from write start |
| Unicast | 188 bytes/datagram、SO_SNDBUF要求16 KiB / requested SO_SNDBUF 16 KiB |
| Multicast | 従来どおり1316 bytes集約とTTL / existing 1316-byte coalescing and TTL |
| 監視 / Monitoring | D-Bus・設定を別スレッドで照会 / separate D-Bus and configuration polling threads |
| 統計 / Statistics | cloudlogへ約5秒ごと / cloudlog approximately every 5 seconds |

RGBA → FFmpeg stdin → libx264 / MPEG-TS → stdout (`pipe:1`) → Python socketの経路を維持します。送信側FFmpegは`file`・`pipe`対応だけで動作し、UDPプロトコルを必要としません。Linuxではstdoutのread可能通知で送信を再開し、固定bitrate pacingは加えません。SCHED_OTHERを維持し、意図的なnice +10設定を廃止します。GPU readbackは同期方式のままです。

The path remains RGBA → FFmpeg stdin → libx264 / MPEG-TS → stdout (`pipe:1`) → Python socket. Sender FFmpeg needs only `file` and `pipe`, not UDP protocol support. Linux waits for stdout readability without fixed-bitrate pacing. The worker stays on SCHED_OTHER and no longer sets nice +10. GPU readback remains synchronous.

75msと100msは個別の処理上限で、合計100ms以下を保証しません。VBV値も実際の待ち時間を表すものではありません。短い期限による再起動、小さい送信バッファでの破棄、188-byte送信のCPU・無線負荷を統計で確認してください。

The 75 ms and 100 ms limits apply to separate stages and do not guarantee a total below 100 ms. VBV size is not a measured buffering delay. Check statistics for restarts from shorter deadlines, drops from the smaller send buffer, and CPU/Wi-Fi overhead from 188-byte datagrams.

録画`RECORD=1`を優先し、Wi-Fi未接続・消灯時は停止します。OFF時はキャプチャ・送信と定期照会を休止します。配信は暗号化・認証されず、設定画面も含まれます。音声と描画後のデバッグ表示は含まれません。

`RECORD=1` takes precedence. Streaming stops when Wi-Fi is disconnected or the display sleeps. Disabling it pauses capture, transport, and polling. The stream is unencrypted and unauthenticated and includes settings screens. Audio and post-render debug overlays are excluded.

## 検証・実機診断 / Validation and device diagnostics

実機`40e0a4961`ではunicastで画質が大幅に改善し、PIDも安定したとの報告があります。ただし数百ms～約1000msの遅延が残り、1500 kbit/sの方が500より遅いと報告されています。両条件のqdisc backlog・drop等は0でした。今回の実験版による実機改善は未測定です。

Device reports for `40e0a4961` confirm substantially better unicast quality and a stable PID, but hundreds of milliseconds to roughly 1 second of latency remain, with 1500 kbit/s slower than 500. Reported qdisc backlog and drop counters were zero at both rates. Device improvements from this experimental version remain unmeasured.

開発PCでは不規則入力のPTS圧縮を再現し、wall-clock入力でgapが保たれることを500／1500／3000 kbit/sで確認しました。[検証結果・ベンチマーク・実機手順](docs/ui_udp_stream.md)を参照してください。

Development-PC tests reproduced compressed PTS gaps and verified that wall-clock input preserves them at 500/1500/3000 kbit/s. See the [validation results, benchmark, and device guide (Japanese)](docs/ui_udp_stream.md).

実機で最初に見る統計とハードウェアエンコーダの診断（自動採用はしません） / First device statistics to inspect and hardware-encoder discovery (no automatic selection):

```sh
grep -R -a "screen stream latency stats" /data/log 2>/dev/null | tail -30
ffmpeg -hide_banner -encoders 2>/dev/null | grep -Ei 'h264|v4l2|qcom|omx|vaapi'
```

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
