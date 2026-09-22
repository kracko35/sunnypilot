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
4. 以下の受信コマンドを1つだけ起動します。500／1000／1500 kbit/sで、設定変更後約10秒のウォームアップに続けて各30秒以上測定します。 / Start one receiver below. At each of 500/1000/1500 kbit/s, warm up for about 10 seconds after changing settings, then measure for at least 30 seconds.

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
gst-launch-1.0 -v udpsrc address=0.0.0.0 port=12346 buffer-size=65536 caps="video/mpegts,systemstream=(boolean)true,packetsize=(int)188" "!" tsdemux latency=0 "!" h264parse "!" avdec_h264 "!" queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "!" videoconvert "!" autovideosink sync=false
```

PowerShellで改行する場合は行末にバッククォートを使用し、cmd用の`^`は使いません。Windows実機ではautovideosinkのasyncプロパティが使えないため指定しません。上記構成で受信できましたが、遅延はFFplayと大差なかったとの報告です。必要なプラグインと比較手順は[手順書](docs/ui_udp_stream.md#受信と切り分け)を参照してください。

For multiline PowerShell commands, use a trailing backtick, not cmd's `^`. Omit the unsupported autovideosink async property on the tested Windows installation. Device testing confirmed reception with this pipeline, with latency similar to FFplay. See the [guide (Japanese)](docs/ui_udp_stream.md#受信と切り分け) for required plugins and comparisons.

## 実験版の変更 / Experimental changes

| 項目 / Item | 設定 / Setting |
| --- | --- |
| 映像 / Video | 800×480、最大20fps、黒帯で縦横比維持 / up to 20 fps, aspect ratio preserved with black bars |
| Timestamp | FFmpeg入力のwall clock / FFmpeg input wall clock |
| Encoder | libx264 ultrafast, zerolatency, baseline, yuv420p, B=0, GOP=10, lookahead=0 |
| VBV | bitrate / 10（1500 kbit/sなら150 kbit）/ 150 kbit at 1500 kbit/s |
| 古い未送信frame / Stale pending frames | 75ms以上で破棄 / discard at 75 ms |
| stdin書き込み期限 / Write deadline | 書き込み開始から100ms / 100 ms from write start |
| Unicast | 564 bytes/datagram（TS 3個）、SO_SNDBUFはOS既定値 / 3 TS packets, OS-default SO_SNDBUF |
| Multicast | 従来どおり1316 bytes集約とTTL / existing 1316-byte coalescing and TTL |
| 監視 / Monitoring | D-Bus・設定を別スレッドで照会 / separate D-Bus and configuration polling threads |
| 統計 / Statistics | 約5秒ごとのstage時間・I/O待ち・drop率・pipe/socket残量・FFmpeg CPUと終了前finalログ / stage timings, I/O waits, drops, pipe/socket backlog and FFmpeg CPU approximately every 5 seconds, plus final statistics before closing |

RGBA → FFmpeg stdin → libx264 / MPEG-TS → stdout (`pipe:1`) → Python socketの経路を維持します。送信側FFmpegは`file`・`pipe`対応だけで動作し、UDPプロトコルを必要としません。Linuxではstdoutのread可能通知で送信を再開し、固定bitrate pacingは加えません。SCHED_OTHERを維持し、意図的なnice +10設定を廃止します。GPU readbackは同期方式のままです。

The path remains RGBA → FFmpeg stdin → libx264 / MPEG-TS → stdout (`pipe:1`) → Python socket. Sender FFmpeg needs only `file` and `pipe`, not UDP protocol support. Linux waits for stdout readability without fixed-bitrate pacing. The worker stays on SCHED_OTHER and no longer sets nice +10. GPU readback remains synchronous.

75msと100msは個別の処理上限で、合計100ms以下を保証しません。VBV値も実際の待ち時間を表すものではありません。1500 kbit/sの単純換算では188→564 bytesで約997→332 datagrams/sとなります。SO_SNDBUF実値を記録し、Linux outqの取得を100ms間隔に制限します。区間の送信試行が100件以上かつdrop率1%以上なら混雑警告を出し、警告だけではエンコーダを再起動しません。

The 75 ms and 100 ms limits apply to separate stages and do not guarantee a total below 100 ms. VBV size is not a measured buffering delay. At 1500 kbit/s, a payload-only estimate falls from about 997 to 332 datagrams/s when moving from 188 to 564 bytes. Actual SO_SNDBUF is logged, and Linux outq sampling is limited to once per 100 ms. A window with at least 100 send attempts and 1% drops produces a congestion warning without restarting the encoder solely for that warning.

録画`RECORD=1`を優先し、Wi-Fi未接続・消灯時は停止します。OFF時はキャプチャ・送信と定期照会を休止します。配信は暗号化・認証されず、設定画面も含まれます。音声と描画後のデバッグ表示は含まれません。

`RECORD=1` takes precedence. Streaming stops when Wi-Fi is disconnected or the display sleeps. Disabling it pauses capture, transport, and polling. The stream is unencrypted and unauthenticated and includes settings screens. Audio and post-render debug overlays are excluded.

## 検証・実機診断 / Validation and device diagnostics

実機`fe6f5b329`では500 kbit/sで約800ms、1500で約1300msの遅延が報告されました。SO_SNDBUF実値32768に対しoutq peakは33280、EAGAINによる大量dropが発生した一方、qdisc backlog・drop等は0でした。今回は188-byte送信と16 KiBの送信バッファ指定を見直します。この変更後の実機改善は未測定です。

Device reports for `fe6f5b329` show roughly 800 ms at 500 kbit/s and 1300 ms at 1500. Actual SO_SNDBUF was 32768 with an outq peak of 33280 and substantial EAGAIN drops, while qdisc backlog and drop counters were zero. This revision replaces 188-byte datagrams and the forced 16 KiB send buffer. Device improvements after this change remain unmeasured.

実UI配信では小さいTS/PES observerにより、capture→PES初観測→その先頭TSを含むUDP送信までを計測します。入力順序・PTS・TS連続性を検査し、入力数とPES数が釣り合う区間だけ確定します。未確定記録は最大32件・2秒、stage標本は最大200件。同期を失った場合はdiag_active=0として次のFFmpegプロセスまで対応付けを休止し、配信は継続します。映像保存・再復号・本番UIへの識別子描画は行いません。

During UI streaming, a small TS/PES observer measures capture → first PES observation → sending the datagram containing its first TS packet. It checks input order, PTS, and TS continuity, and confirms intervals only when input and PES counts balance. Unconfirmed records are limited to 32 and two seconds, with at most 200 samples per stage. Loss of synchronization sets diag_active=0 and suspends pairing until the next FFmpeg process; streaming continues. No video is saved or decoded, and no markers are drawn on the production UI.

別途、合成画像の識別子・復号PTSを使う厳密なベンチマークと本番observerを500／1000／1500／3000 kbit/sで照合します。GPU・encoder・受信設定や75ms／100msの期限は維持しています。先頭datagramのsendto完了は、frame全体の送信完了やPC表示完了ではありません。[検証結果・ベンチマーク・実機手順](docs/ui_udp_stream.md)を参照してください。

The separate benchmark validates the production observer against decoded synthetic frame IDs and PTS at 500/1000/1500/3000 kbit/s. GPU, encoder, receiver settings, and the separate 75 ms / 100 ms limits remain unchanged. Completion of the first datagram's sendto is not completion of the whole frame or PC display. See the [validation results, benchmark, and device guide (Japanese)](docs/ui_udp_stream.md).

実機で最初に見る統計とハードウェアエンコーダの診断（自動採用はしません） / First device statistics to inspect and hardware-encoder discovery (no automatic selection):

```sh
HASH=$(git rev-parse HEAD)
grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null | grep "screen stream latency stats:" | grep "bitrate=500 " | tail -10
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
