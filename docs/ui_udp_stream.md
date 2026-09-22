# UDP画面配信: ultra low latency experimental

## 目的と実機状況

comma 3X → Windows PCのunicastを基本候補とし、glass-to-glass 100ms以下を目標に計測と調整を行う実験版。100ms達成を保証するものではなく、本変更後の実機遅延は未測定。MIB・MOST・AIDは変更しない。H.264 / MPEG-TS / UDPを維持し、multicastも残す。

利用者による`fe6f5b32953171882c9bd13968998a4b0d906887`の実機報告:

- comma `192.168.4.38` → PC `192.168.4.44:12346` のunicastで画質が大幅改善。従来のmulticastではPacket corrupt・H.264 macroblockエラーが多かった。
- FFmpeg PIDの安定化とunicastの画質改善を維持する。
- glass-to-glassは500 kbit/sで約800ms、1500 kbit/sで約1300ms。
- `socket_sndbuf=32768`に対し`socket_outq_peak=33280`。16 KiB要求による小さいsocket容量をほぼ使い切り、`last_errno=11`（EAGAIN系）の大量dropが発生した。
- 1000 kbit/sは成功約75,000／破棄約6,800（試行数に対して約8.3%）、1500は成功約65,000／破棄約10,600（約14.0%）。概数・別セッションのため正確な比較には同じ長さのwindowが必要。
- 両ビットレートでqdiscのdropped / overlimits / requeues / backlogは0。
- Windows FFplayの`-avioflags direct`で “Part of datagram lost due to insufficient buffer size” と映像破損が多発。このオプションは推奨から除外する。

500／1500の`tc -s qdisc show dev wlan0`はdropped 0 / overlimits 0 / requeues 0 / backlog 0b 0pだった。qdiscは主要な滞留箇所には見えないが、socket outqには混雑が観測されている。両者は異なる段階の計測であり、qdiscが0でもsocket・無線ドライバー・受信側の待ちを否定できない。outqとSO_SNDBUFも単純な映像payload容量として換算しない。

| 実機の区間平均 | 1000 kbit/s付近 | 1500 kbit/sの悪化区間 |
| --- | --- | --- |
| capture | 約3.5～4ms | 概ね3～4ms |
| queue_age | 約5～8ms | 約29～34ms |
| stdin_write | 約16～24ms | 約47～51ms |
| frame_age_written | 約22～32ms | 約76～85ms、最大110～138ms |

GPU readbackは約2.5～3msで、現状の800～1300msの主因には見えない。高bitrate → stdout量増加 → UDP送信負荷／socket混雑 → stdoutの読み出し停滞 → FFmpeg内部／stdinの詰まり → queue age増加という仮説を検証する。非ブロッキングsendto自体は混雑時に待たず破棄するので、この因果関係を既に証明したとは扱わない。

## ブランチと導入

作業ブランチは`udp-screen-streaming`のみ。起点はupstream/masterの`a5f44653d7f43ad57fef2f546f3916ec4cbf3c56`。masterへmergeしない。実機テスト用ブランチとcherry-pick先は別途決定する。

導入先を決めてから、停車・車両電源OFFで現在のコミットと作業差分を記録する。既存の変更を保存し、選んだソースのsubmodule・LFS取得と通常のビルドを完了する。配信Paramキーが未登録の古いビルドから導入する場合はC++ライブラリも再ビルドする。今回の変更で新たなParamは増やさない。

```sh
cd /data/openpilot
git status --short
git branch --show-current
git rev-parse HEAD
ffmpeg -hide_banner -encoders 2>/dev/null | grep -Ei 'h264|v4l2|qcom|omx|vaapi'
ffmpeg -hide_banner -protocols
ffmpeg -hide_banner -muxers 2>/dev/null | grep mpegts
```

必要なのはlibx264、MPEG-TS、file/pipe。comma標準FFmpegのUDP非対応でもPython socketで送信する。hardware H.264 encoderは実機のcapability確認後に判断し、自動選択しない。Windowsの`.venv`は端末へコピーしない。

設定 → トグル → UDP Screen Streaming（日本語: UDP画面配信）をONにする。Destination AddressをPCのWi-Fi IPv4、ポート12346、最初は500 kbit/sに設定。1000、1500へ変更して各条件で約10秒ウォームアップし、その後30秒以上測定して5秒statsを複数採取する。保存直後のsenderとFFmpegの再生成は正常であり、試験途中の障害再起動と区別する。

## 設定と互換性

| 英語UI | 永続キー | 既定値 | 範囲 |
| --- | --- | --- | --- |
| Destination Address | ScreenStreamAddress | 239.255.42.99 | 通常のIPv4 unicastまたは224.0.1.0～239.255.255.255 |
| UDP Port | ScreenStreamPort | 12346 | 1～65535 |
| Bitrate (kbit/s) | ScreenStreamBitrate | 1500 | 250～8000 |
| Multicast TTL | ScreenStreamTtl | 1 | 1～255、unicastでは送信に使用しない |

ScreenStreamEnabledの初期値はOFF。ON時だけ4項目を表示し、録画中は編集できない。保存値は再起動後も維持する。0/8、127/8、link-local、予約済みIPv4、255.255.255.255、224.0.0/24、IPv6、ホスト名、URL、ポート・クエリ付き入力は拒否する。サブネットごとのdirected broadcast判定は行わないので、PCのホストアドレスを指定する。

英語UI原文を本家のtr / tr_noopとPOローダーで翻訳し、日本語を含む12言語を維持。新たな実験用UI項目は追加せず、定数を調整する。READMEのみ英日併記、コメント・手順書は日本語。

## Timestampの検証

従来はrawvideoの`-framerate 20 -i pipe:0`が入力フレーム数からPTSを生成していた。FFmpegより手前でフレームを間引くと、実時間の空白がTSのPTSに残らない。開発PCの実FFmpegで、約0 / 50 / 100 / 300 / 350msの入力に対して、従来相当のPTSが0 / 50 / 100 / 150 / 200msになることを再現した。

`-use_wallclock_as_timestamps 1`を`-i pipe:0`より前へ加えると、PTSに200ms付近の入力gapが残った。500 / 1500 / 3000 kbit/sで確認する自動テストを追加した。初回実測では0 / 50 / 100 / 300 / 350ms、別試行では0 / 50 / 100 / 250 / 300msとなった。入力開始とFFmpegによる読み取りの差、OS scheduling、20fpsのtime baseによる量子化を含むため、相対時刻の許容差を75msとしている。

wallclockのみ、wallclock＋fps_mode passthrough、さらにcopyts / start_at_zeroの組合せを比較したが、対象MPEG-TS経路の不規則入力に追加オプションは不要だった。最終コマンドにはwallclockのみを採用し、出力の固定フレームレートは指定しない。[FFmpeg形式オプション](https://ffmpeg.org/ffmpeg-formats.html#Format-Options)、[timestampと同期オプション](https://ffmpeg.org/ffmpeg.html#Advanced-options)を参照。

これは「フレーム数だけでPTSが進む」挙動の再現であり、実機で観測した約1000msの唯一の原因を証明したものではない。また、wallclockはFFmpegが入力を読む時刻であってGPUキャプチャ時刻ではない。Pythonパイプ、FFmpeg内部、NTP等によるシステム時計補正の影響は残る。キャプチャ開始時刻は別途monotonicで計測する。

## エンコードと期限

以下は`fe6f5b329`で導入済みで、今回も維持する。

| 項目 | 導入前 | 維持する値 |
| --- | --- | --- |
| 入力PTS | 受信フレーム数 / 20 | 入力wallclock |
| x264 preset | veryfast | ultrafast |
| x264 params | repeat-headers=1 | sync-lookahead=0:rc-lookahead=0:sliced-threads=1:repeat-headers=1 |
| VBV bufsize | bitrate / 3 | bitrate / 10 |
| FRAME_MAX_AGE | 250ms | 75ms |
| PIPE_WRITE_TIMEOUT | 500ms | 100ms |
| スケジューラ | SCHED_OTHER、nice +10 | SCHED_OTHER、niceを変更しない |

維持: 800×480、最大20fps、libx264、zerolatency、baseline、yuv420p、B=0、GOP=10、keyint_min=10、sc_threshold=0、threads=2、muxdelay=0、muxpreload=0、flush_packets=1、MPEG-TSのpipe:1出力。500 / 1500 / 3000 kbit/sのVBVは50 / 150 / 300 kbitで、開発PCのlibx264ではエラーや最小値へのclamp警告は出なかった。VBVはrate-controlの容量であり、その値を実際の100ms待機と解釈しない。

75ms以上古い未送信frameは、FFmpegへ入れる前に丸ごと破棄する。rawvideoを途中まで書いた後に100msの期限へ達した場合は、境界を壊さないようFFmpegごと再生成する。2つの上限は独立し、合計100ms以下を保証しない。今回captured + 100msの絶対期限は追加しない。まず送信負荷と混雑を改善し、frame ageと再起動頻度を取り直す。

配信ワーカーでSCHED_OTHERへ変更してからmonitor・sender・FFmpegを起動する。意図的なnice +10を廃止し、nice 0への昇格操作も行わない。親プロセスのniceを継承するため、通常priorityで起動していることは実機で確認する。CAP_SYS_NICEを必要とするpriority上昇やrealtime化は行わない。

## UDP送信とsocket計測

送信先の`IPv4Address.is_multicast`で判定する。両方式で非ブロッキングsocketとsendtoを使用し、connect / SO_BINDTODEVICE / アプリ側の固定bitrate pacingは追加しない。

| 項目 | unicast | multicast |
| --- | --- | --- |
| 送信元 | bind((Wi-Fi IPv4, 0)) | IP_MULTICAST_IF=Wi-Fi IPv4 |
| TTL | OS既定値、UI設定は無視 | IP_MULTICAST_TTL=保存値 |
| 通常UDP payload | 564 bytes（TS 3個） | 1316 bytes（TS 7個） |
| SO_SNDBUF要求 | 明示変更なし、OS既定値 | 明示変更なし、OS既定値 |

読み取り境界とTS境界が異なっても順序どおり保持する。unicastはTS 3個、multicastは7個へ集約する。各sendtoへ独立したbytesを渡し、offsetを進め、バッチ最後にbytearray先頭を1回だけ削除する。正常EOFだけ残った完全なTSを送信し、停止時の末尾や不完全なTSは破棄する。一時的なEAGAIN / ENOBUFS等ではデータグラムだけを破棄してFFmpegを維持し、致命的障害は再生成する。

TSの欠落はH.264の破損につながるため、大量dropを正常と扱わない。約5秒の区間で送信試行100件以上かつdrop率1%以上なら`screen stream UDP congestion:`を出す。終了前の短い区間にも同じ基準を適用する。警告だけで再起動せず、既存の`screen stream UDP drops:`（最大約5秒に1回）も残す。

Linuxでは非ブロッキングstdoutのEAGAIN後にselectで可読通知を待つ。50msは停止確認のための最大待機で、データ到着時は即時に起きる。Windowsの匿名パイプはselect非対応なので、開発PCでは1ms待機を使う。

強制SO_SNDBUF=16 KiBを撤廃し、getsockoptでOS既定の実値を読み、起動・統計ログへ出す。変更後のcomma実値は未測定。容量を戻すことでdropの改善を狙うが、bufferが大きいほど遅延が小さいとは限らないのでoutq・frame age・端末間遅延を併せて確認する。[socket(7)](https://man7.org/linux/man-pages/man7/socket.7.html)

LinuxのTIOCOUTQはstdout読み取りバッチの送信後と統計出力時に呼び出し機会を持つが、共通の100ms制限によりioctlは最大約10回/秒に抑える。packetごとには取得しない。アイドル時の厳密な100ms周期は保証しない。取得失敗は配信障害にせずcurrent=Noneとする。peakはsender生成以降、window_peakは前回統計以降のサンプル最大値で、統計出力時にwindowだけリセットする。サンプルがなければNone。未観測の瞬間ピークや無線ドライバー・AP・受信側のキューは含まない。[udp(7)](https://man7.org/linux/man-pages/man7/udp.7.html)

| TS個数 | payload bytes | 1500 kbit/sのdatagrams・sendto回数/秒（概算） | 211 TSを送るテストでの回数 |
| --- | --- | --- | --- |
| 1 | 188 | 997 | 211 |
| 2 | 376 | 499 | 106 |
| 3（既定） | 564 | 332 | 71 |
| 7 | 1316 | 142 | 31 |

概算はbitrate / (8 × payload)、ネットワーク等のoverheadは別。各datagramでsendtoを1回行う。564 bytes分の連続入力時間は1500 kbit/sで約3ms、500で約9msとなり、送信回数と集約待ちの折衷とする。実際の映像出力はVBR・バーストなので、この値は待ち時間の上限ではない。ベンチマークは実際のTS長から全4候補の送信回数も計算する。

実機A/Bは`openpilot/system/ui/lib/screen_stream.py`のUNICAST_TS_PACKETS_PER_DATAGRAMだけを1／2／3／7へ変更し、他条件を固定する。追加UIは設けない。FRAME_MAX_AGE、PIPE_WRITE_TIMEOUT、OUTQ_SAMPLE_INTERVAL、LATENCY_STATS_INTERVAL、UDP_CONGESTION_RATIO_PCT、UDP_CONGESTION_MIN_ATTEMPTS、ENCODER_PRESETも先頭へまとめる。環境変数による暗黙のoverrideは追加しない。

## 監視とキャプチャ

`screen_stream_monitor.py`のCachedQueryをネットワークと設定に1つずつ使用する。照会時はロックを持たず、結果・最後の正常値・error・generation・連続失敗数を短いロックで更新する。配信ワーカーはsnapshotだけを読み、D-Busと設定全体の読み取りを行わない。配信ON/OFFのBOOL確認はワーカーで継続する。

ネットワーク照会は接続中5秒・未接続時1秒、設定確認は1秒。D-Bus各リクエストの250ms上限、last-known-good、一時失敗時のPID維持、成功したNoneによる停止、tuple変更による再生成、警告の10秒間隔を維持する。OFF・消灯では照会を休止し、進行中の古い照会結果はepochで排除する。終了時はdaemonへ停止通知し、各200msまでjoinする。ブロック中のD-Bus呼び出し自体は強制中断せず、戻った後に結果を破棄して終了する。

`ScreenStreamCapture.capture()`はGPU操作前のmonotonic時刻を記録し、submit(data, captured=...)へ渡す。GPU縮小、同期load_image_from_texture、bytesコピーを区別して計測する。queue_ageはキャプチャ開始から取り出しまでなのでreadback時間も含む。frame_age_writtenはstdinへの全書き込み完了までを含む。

GPU readbackは同期方式を維持し、今回この経路を変更しない。実機でcapture約3～4ms、readback約2.5～3msなので、PBO・zero-copyは追加せず計測を維持する。GPU描画命令の非同期実行による待ちはreadback側の時間へ現れることがある。

キャプチャ位置は`application.py`のend_drawing後を維持する。RenderTextureはend_texture_mode直後に完成するが、そこへ同期readbackを移すとローカル画面表示を遅らせる可能性がある。効果を実機で確認できないため移動せず、描画途中のtextureは読まない。描画完了からキャプチャ開始までの表示待ち時間は今回のcapture統計には含まない。

## 実機での再起動診断と遅延統計

```sh
HASH=$(git rev-parse HEAD)
for BITRATE in 500 1000 1500; do
  grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null \
    | grep "screen stream latency stats:" | grep "bitrate=$BITRATE " | tail -10
done
grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null | grep "screen stream UDP drops" | tail -30
grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null | grep "screen stream UDP congestion:" | tail -20
grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null | grep "screen stream latency final:" | tail -30
grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null | grep -E "screen stream (restart|started)" | tail -30
grep -R -h -a "\"commit\": \"$HASH\"" /data/log 2>/dev/null | grep "screen stream restart:" | tail -20
pgrep -a -x ffmpeg
tc -s qdisc show dev wlan0
```

実機の作業ツリーがクリーンで、新コミットから起動済みであることを先に確認する。過去コミットの集計を混ぜない。起動ログ: PID、mode、Wi-Fi tuple、destination、bitrate、TTL、socket_sndbuf実値、payload_size、preset。statsは配信ワーカーから約5秒ごとにcloudlogへ出し、毎frameのログは行わない。OFF・消灯・待機中は定期ログを出さない。

| 統計 | 意味 |
| --- | --- |
| capture_count | 正常にキャプチャを完了した数 |
| submitted_count | readyかつ正常サイズでキューへ渡した数 |
| queue_replaced_count | 未処理frameを新しいframeで置換した数 |
| stale_drop_count | 75ms以上古く、書き込み前に破棄した数 |
| frames_written | stdinへ完全に書き終えた数 |
| capture_avg_ms / max_ms | GPU操作開始から読み出し・コピー・画像解放まで |
| gpu_scale_avg_ms / max_ms | 縮小先確保と縮小描画のCPU側処理時間 |
| readback_avg_ms / max_ms | load_image_from_textureの同期呼び出し時間 |
| bytes_copy_avg_ms / max_ms | bytesへのコピー時間 |
| queue_age_avg_ms / max_ms | キャプチャ開始からキュー取り出しまで。破棄frameも含む |
| stdin_write_avg_ms / max_ms | 書き込み開始から完了または失敗まで |
| stdin_write_syscalls / stdin_bytes / stdin_blocked_events | os.write試行数（EAGAINを含む）／成功bytes／BlockingIOError数 |
| stdin_blocked_wait_ms / avg_ms / max_ms | BlockingIOError後のselect等の待ちの合計／1回平均／最大 |
| frame_age_written_avg_ms / max_ms | キャプチャ開始から完全書き込み終了まで |
| sender_datagrams / sender_drops / sender_bytes | senderの累積送信成功数・破棄数・成功bytes |
| sender_drop_ratio_pct | 累積drop /（成功 + drop）×100 |
| sender_datagrams_delta / sender_drops_delta / sender_bytes_delta | 前回統計以降の成功数・破棄数・成功bytes |
| sender_drop_ratio_window_pct | 区間drop /（区間成功 + 区間drop）×100。試行0なら0 |
| stdout_bytes_delta / stdout_chunks_delta / stdout_bytes_per_sec | FFmpeg stdoutから読み取れたbytes・read回数・bytes/秒 |
| datagrams_per_sec / sender_drops_per_sec / sender_bytes_per_sec | 区間の成功datagrams/秒・破棄/秒・成功bytes/秒 |
| stats_window_s / capture_fps / submitted_fps / frames_written_fps | 実際の区間秒数とcapture・submit・完全stdin書き込みの各回数/秒 |
| socket_sndbuf | getsockoptで取得した実容量 |
| socket_outq_current / socket_outq_peak / socket_outq_window_peak | 最新サンプル／sender生成後の最大／前回stats以降の最大。未観測はNone |
| pid / bitrate / mode / window_id | 現在のエンコーダと送信設定、プロセス内の統計区間番号 |

capture～frames_writtenと時間統計は前回ログからの区間値で、各時間の平均はその測定サンプル数を分母にする。rateは固定の5秒ではなく実経過時間で割る。senderの累積値と区間差分を両方残す。送信と統計のスレッドは並行するため、カウンターの採取境界や処理段階をまたぐframeによって同一区間の数は完全には一致しない。

終了・設定変更・再起動ではencoder/senderを閉じる前に`screen stream latency final: reason=...`を1回出し、5秒未満の区間も保存する。停止済みの状態でcloseを繰り返しても重複しない。採取後から送信スレッド停止までのごく短い差はあり得る。新しいencoderで区間統計とsender累積値をリセットする。ログ採取の例外が起きても停止処理は続ける。

500→1000→1500でstdout量・drop率・outq・stdin_write・queue_ageが同じ区間で増えるか確認する。stdout_bytes_per_secは「既に読み取れた量」であって未読キュー容量ではない。低い値だけでエンコーダが遅いと断定しない。queue_replaced増加、frames_written_fps低下、stdin_write悪化も合わせて見る。後述のパイプ単独とUDP経由のフレーム診断で差が出れば、送信処理からの逆圧の仮説を補強できる。

## 実UI配信のstage観測

`4d032be`で送信側の564-byte集約・OS既定SO_SNDBUF・100ms outq取得・区間drop統計を導入した。今回それらの値とGPU、FFmpegコマンド、20fps、75ms／100ms期限、receiverを固定し、未観測だったstdin→PES→UDPの区間を追加する。変更後のcomma実測はまだなく、送信混雑の改善量も未確認。

本番モジュール`screen_stream_latency.py`はtestsをimportしない。UIで受理したframeには単調増加のsequenceを付け、FFmpegへの書き込み開始前にsequence・capture開始・dequeue・stdin開始を登録する。書き込み完了は後から結合する。キュー置換やstale破棄で書き込まなかったsequenceの空白は許容し、書き込み対象の順序でPESへ仮対応する。

TS observerは188-byte packetのヘッダー、adaptation長、映像PIDのcontinuity counter、PUSI、映像PES開始コード、PTSだけを読む。未完成TSは最大187 bytesとその観測時刻のみ保持し、H.264 payloadの解析・再復号・映像保存・本番UIへのmarker描画は行わない。PESヘッダーは現在のFFmpegと同様に先頭TS内に収まる出力に限定する。read境界で分かれたヘッダーも処理し、PES先頭byteを含んだ最初のread時刻を使用する。

対応の前提は単一映像PID・H.264・B=0・1入力につき1 PES・入力順の出力。次の条件を検査する。

- sequenceが逆行・重複せず、入力時刻の順序が正しい。
- TSの同期・エラーフラグ・連続性に異常がなく、映像PIDが変わらない。
- PTSが前進する。33 bitの正常wrapは許容する。gapは2秒以下で、入力開始間隔との差は100ms以内（入力読み取り時刻と20fps量子化の差を含む）。
- 未対応の入力なしにPESが来ない。入力数とPES数が釣り合い、書き込み・対象datagramの送信結果が揃うまで区間の標本を確定しない。
- 未確定記録は最大32件、最古の入力開始から2秒以内。overflow時は古い記録を黙って捨てて対応をずらさない。

1 frameが欠落して後続PESが一つ前の入力へ仮対応しても、未対応入力が残る間は標本を公開しない。連続した遅延で入力/PESの釣り合いが戻らない場合も、2秒または32件で計測を断念する。この保守的な条件により、混雑時には数値の代わりに「計測不能」が出る場合がある。

異常時は`diag_sync_lost`を1加算し、未確定記録を破棄、`diag_active=0`と固定の`diag_reason`を出す。通常の送信は継続し、observerを理由にencoderをrestartしない。途中のPESから再同期を推測せず、次のFFmpegプロセスで新しいobserverを作る。終了時の未確定入力もfinalへ`unfinished_at_close`として残すが、これは通常停止で出力を打ち切った場合にも起きる。

この方式は映像のidentityを厳密に証明するものではない。前提外の同数の欠落と重複など、ヘッダーと数だけでは検出できない組合せはある。固定した本番コマンドについて、別途markerを復号する厳密な試験で順序対応を検証する。`diag_active=0`、`diag_pending`増加、標本0の区間を健全な0msと解釈しない。

| 新しい統計 | 意味 |
| --- | --- |
| encoder_pes_samples / pes_events | 区間で対応を確定したframe数／observerが同期中に受けたPES数 |
| diag_sync_lost / diag_active / diag_reason / diag_pending | 区間内の同期喪失数／有効状態／最後の理由／未確定記録数 |
| pes_events_per_sec / frames_paired_per_sec | PES観測数／確定frame数を区間実時間で割った値 |
| capture_to_pes_avg_ms / p95_ms / max_ms | GPU操作前のcapture開始→PES先頭byteのstdout初観測 |
| stdin_start_to_pes_avg_ms / p95_ms / max_ms | stdin開始→同じPES初観測。主要指標 |
| stdin_complete_to_pes_avg_ms / p95_ms / max_ms | stdin完了→PES初観測。readerが先行した負値も保持 |
| pes_to_send_attempt_avg_ms / p95_ms / max_ms | PES初観測→先頭TSを含むdatagramのsendto開始。dropも含む |
| pes_to_send_avg_ms / p95_ms / max_ms | PES初観測→同datagramのsendto成功復帰 |
| capture_to_udp_send_avg_ms / p95_ms / max_ms | capture開始→同datagramのsendto成功復帰 |
| 各stage名_samples | その平均・p95・最大に使った標本数 |
| frame_first_datagram_drop_count | 対応確定frameの先頭TSを含むdatagramが送信失敗した数 |
| sendto_calls / sendto_eagain_count | 区間のsendto試行数／EAGAIN系の回数 |
| sendto_avg_us / sendto_max_us | sendto呼び出し時間。警告ログ・observer処理は含まない |
| stdout_read_calls / stdout_eagain_count | read試行数（EOFも含む）／EAGAIN回数 |
| stdout_wait_ms / stdout_wait_max_ms | EAGAIN後のselect等の待ち合計／1回最大 |
| stdout_bytes / stdout_chunks | 同区間に読み取れたbytes／空でないread数。既存deltaの別名 |
| stdout_pipe_available_current / stdout_pipe_available_window_peak | Linux FIONREADの最新／区間内の観測最大bytes |
| ffmpeg_cpu_pct / ffmpeg_rss_kb | FFmpeg子プロセスのCPU割合／RSS。未取得はNone |

stageごとに最大200標本を保持し、約5秒ごとの出力でリセットする。200件を超えた場合は直近200件の平均・p95・最大となる。p95はnearest-rank方式。標本0なら時間はNone。`encoder_pes_samples`は区間の全確定数なので、上限を超える場合やdropがある場合に個別stageのsamplesと一致しない。dropしたdatagramは成功送信までの時間へ含めず、attempt時間とdrop件数に残す。windowをまたいだ未確定記録は、確定した側のwindowへまとめる。

PESを含むTSの絶対byte offsetを追跡し、564／1316-byte集約やread境界と独立してdatagramへ対応付ける。sendto開始と復帰を記録するが、これは**frameの最初のTSを含むdatagram**の時間であり、frame全体のencode・送信完了や無線送信完了ではない。

stdout待ちは「その時点でreadできなかった時間」で、encoder CPUだけを示さない。20fpsの正常なframe間待ちも含むため、合計がwindowの大半でもそれだけで遅延異常とは判断しない。stdinのblocked待ちはFFmpegの入力取り込みが進まない状況を示すが、encoder CPU不足と下流の逆圧の両方で起こり得る。CPU・pipe残量・outq・stage時間を同じwindow_idで合わせて判断する。

FIONREADは既存outqと共通の100ms制限を使い、各種類のioctlを最大約10回/秒に抑える。readバッチ処理後とstats時に取得するため、瞬間最大値の保証はない。非Linux・未対応・fd終了・取得失敗はNone、配信は継続する。window_peakだけstatsごとにリセットする。[Linux pipeのFIONREAD](https://man7.org/linux/man-pages/man7/pipe.7.html)を参照。

FFmpeg CPUはstatsとfinalの時だけ`/proc/<pid>/stat`を読み、utime+stime差分をCLK_TCKと実経過時間で割る。1コアを100%とし、複数コア使用時は100%を超え得る。初回は基準値を取るためCPU=None。PIDのstarttimeで再利用を検査し、RSS概算はresident pages×page sizeからKiBにする。PID消失・解析失敗はNoneにして次回基準を取り直す。[Linux proc_pid_stat](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)の定義に従う。

計測負荷は、小さいヘッダー走査、少数の単調時計読み取り、固定容量の記録へ限定する。stdinはframeごとに一度だけ集計ロックを取り、PESを含まないdatagramではobserverロックを取らない。PES解析・p95ソート・/proc読取を入力側との共有ロックの外へ置く。毎frame／packetのログは追加せず、既存の約5秒statsとfinalへ集約する。I/O countersは送信スレッドの累積値の差分なので、並行採取の境界でcall数と時間が隣接windowへ分かれることはある。OFF／idleではobserver、sender、/proc照会を動作させない。

stageの読み方はcapture→queue取り出し→stdin開始→stdin完了→PES初観測→先頭datagramのsendto復帰。既存queue_ageはcapture時間を含むのでcaptureと単純加算しない。また`glass-to-glass - capture_to_udp_send`はWi-Fi＋PC側に加え、残りのframe bytesの出力・送信と測定起点の違いも含む概算で、ネットワーク単独の遅延ではない。

## 受信と切り分け

PowerShellでは以下を1行ずつ実行する。複数行に分けるなら行末はバッククォートを使用し、cmd用の`^`は使わない。受信プロセスは1つだけ起動する。

通常受信と低遅延FFplay:

```powershell
ffplay "udp://0.0.0.0:12346"
ffplay -max_delay 0 -fflags nobuffer -flags low_delay -framedrop -probesize 4096 -analyzeduration 0 "udp://0.0.0.0:12346"
```

multicast比較時は本体の送信先を239.255.42.99へ変更し、PCのURLを`udp://239.255.42.99:12346?localaddr=192.168.4.44`へ変更する。unicastの特定IF待受は`udp://0.0.0.0:12346?localaddr=192.168.4.44`。IPは実際のPCの値に置換する。

`-avioflags direct`は追加しない。fifo_size / overrun_nonfatalも比較の基本条件では指定しない。probesizeは4096を開始点とし、32へ極端に下げない。映像が検出できなければ通常受信に戻す。max_delay=0によるUDP並べ替え待ち無効化の公式説明は[RTSP節](https://ffmpeg.org/ffmpeg-protocols.html#rtsp)であり、raw MPEG-TS/UDPで同じ効果があるとは断定しない。

GStreamer（必要なudpsrc・tsdemux・h264parse・avdec_h264・queue・videoconvert・autovideosinkを導入済みの場合）:

```powershell
gst-launch-1.0 -v udpsrc address=0.0.0.0 port=12346 buffer-size=65536 caps="video/mpegts,systemstream=(boolean)true,packetsize=(int)188" "!" tsdemux latency=0 "!" h264parse "!" avdec_h264 "!" queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "!" videoconvert "!" autovideosink sync=false
```

[tsdemux](https://gstreamer.freedesktop.org/documentation/mpegtsdemux/tsdemux.html)のlatency既定値は700msのため0を明示する。[queue](https://gstreamer.freedesktop.org/documentation/coreelements/queue.html)は復号後に置き、古い復号済みframeを落とす。圧縮H.264やTSの途中をqueueで間引かない。udpsrcの64 KiBは受信バッファ要求値で、実機のlossと併せて確認する。sync=falseは時計待ちを避けるが、どのreceiverでも100ms達成を保証しない。

利用者のWindows環境でautovideosinkのasyncプロパティ指定は`no property "async"`で失敗したため削除した。上記のtsdemux latency=0・復号後最新1frame・sync=false構成は実機受信でき、glass-to-glassはFFplayと大差なかった。FFplay固有bufferingだけを800～1300msの主因とは見なしにくい。共通の受信・復号・表示待ちまで否定した結果ではない。

## 実機A/Bと遅延測定

同じAP、機器位置、UI操作、PC電源設定、画面リフレッシュレートを固定する。500／1000／1500 kbit/sの各条件で設定変更後約10秒ウォームアップし、その後最低30秒ずつ測定する。現在のcommitの5秒statsを複数採る。PID・drop・遅延が安定したら5分測定を3回以上繰り返す。通常の設定変更時だけPIDが変わり、測定中は安定していることを確認する。ログは同じlatency prefixの1行へ集約しており、別のtransport prefixは追加していない。

1. unicast＋通常FFplay。
2. 同じunicast＋低遅延FFplay。細分化するならprobesize / analyzedurationを固定した基準へmax_delay、次にnobuffer / low_delay / framedropを加える。directは試験項目から除外する。
3. 同じunicast＋GStreamer。
4. 必要に応じて同一bitrate・同一receiverのmulticastも比較する。方式でpacketサイズも異なるため、純粋なネットワーク方式だけの差とは解釈しない。

ログ行数を比較する場合はFFplayへ`-loglevel repeat+warning -nostats`を追加し、`2> unicast-500-1.log`のようにstderrを保存する。PowerShellで集計する例:

```powershell
@(Select-String -Path unicast-500-1.log -Pattern 'Packet corrupt').Count
@(Select-String -Path unicast-500-1.log -Pattern 'error while decoding MB|corrupted macroblock').Count
```

これはログ行数で、UDP欠落数や破損フレーム数そのものではない。開始直後の途中参加と継続破損を区別し、集計期間を揃える。

comma画面とPC画面を同時にスマートフォンの60fps以上で撮影し、同一UI変化のframe差を測る。`遅延ms = frame差 / 撮影fps × 1000`。60fpsで6frameなら100ms。可変fps動画では時刻を使用する。各条件10サンプル以上を採り、中央値・最大値・標本数・撮影fpsを記録する。60fpsの1frameは約16.7msなので、小さい差を過大評価しない。初回映像が出るまでの時間は定常時遅延と分ける。

比較表は同じreceiver・AP・UI操作で記入する。commit、測定日時、receiverコマンド、各条件の区間数も併記する。

| 計測項目 | 500 kbit/s | 1000 kbit/s | 1500 kbit/s |
| --- | --- | --- | --- |
| 配信秒数／5秒window数 | 未測定 | 未測定 | 未測定 |
| capture / submitted / frames_written fps | 未測定 | 未測定 | 未測定 |
| queue_replaced / stale_drop | 未測定 | 未測定 | 未測定 |
| capture / readback 平均・最大ms | 未測定 | 未測定 | 未測定 |
| queue_age 平均・最大ms | 未測定 | 未測定 | 未測定 |
| stdin_write 平均・最大ms | 未測定 | 未測定 | 未測定 |
| stdin blocked wait 平均・最大ms／回数 | 未測定 | 未測定 | 未測定 |
| frame_age_written 平均・最大ms | 未測定 | 未測定 | 未測定 |
| capture→PES 平均・p95・最大ms | 未測定 | 未測定 | 未測定 |
| stdin開始→PES 平均・p95・最大ms | 未測定 | 未測定 | 未測定 |
| stdin完了→PES 平均・p95・最大ms | 未測定 | 未測定 | 未測定 |
| PES→UDP send 平均・p95・最大ms | 未測定 | 未測定 | 未測定 |
| capture→UDP send 平均・p95・最大ms | 未測定 | 未測定 | 未測定 |
| diag_active / diag_sync_lost / diag_reason / samples | 未測定 | 未測定 | 未測定 |
| stdout bytes/秒・chunks/区間 | 未測定 | 未測定 | 未測定 |
| 成功datagrams/秒・drop/秒 | 未測定 | 未測定 | 未測定 |
| drop率 区間／累積% | 未測定 | 未測定 | 未測定 |
| socket_sndbuf 実値 | 未測定 | 未測定 | 未測定 |
| outq current / window_peak / peak | 未測定 | 未測定 | 未測定 |
| stdout pipe window peak | 未測定 | 未測定 | 未測定 |
| stdout EAGAIN／wait 合計・最大ms | 未測定 | 未測定 | 未測定 |
| sendto 平均・最大µs／EAGAIN | 未測定 | 未測定 | 未測定 |
| FFmpeg CPU %／RSS KiB | 未測定 | 未測定 | 未測定 |
| qdisc backlog / drops | 未測定 | 未測定 | 未測定 |
| PID安定・restart reason | 未確認 | 未確認 | 未確認 |
| 画質・Packet corrupt／H.264エラー | 未確認 | 未確認 | 未確認 |
| glass-to-glass 中央値／最大ms | 未測定 | 未測定 | 未測定 |
| 診断stdin→stdout 平均／p95／最大ms（別試験） | 未測定 | 未測定 | 未測定 |

今回の成功基準は100ms達成そのものではなく、送信混雑修正を維持し、dropの改善量と実UIのcapture→PES→UDPのstage時間を説明でき、高bitrateで増える区間を特定できること。計測で明らかな性能悪化がない、PID安定・unicast画質維持も確認する。数値を得られない場合もdiag状態を必ず記録する。配信側の継続目標はdrop率ほぼ0、outqが恒常的に満杯にならない、queue replacementがごく少数、frame_age_writtenはできれば50ms以下中心、glass-to-glassが約800／1300msから改善すること。

負荷・温度・UI FPS、OFF/ON、消灯/復帰、Wi-Fi切断/再接続/IP変更、録画との排他も確認する。実機の受信PTSを調べる場合は、例えば短時間だけPCで`ffmpeg -i "udp://0.0.0.0:12346" -t 10 -c copy sample.ts`と記録し、`ffprobe -select_streams v:0 -show_frames -show_entries frame=pts_time -of csv sample.ts`でgapを確認する。採取中は別receiverを同時起動しない。

## 自動テストとベンチマーク

通常の単体テスト:

```sh
python -m unittest openpilot.system.ui.lib.tests.test_screen_stream openpilot.system.ui.lib.tests.test_screen_capture openpilot.system.ui.lib.tests.test_screen_stream_settings openpilot.system.ui.lib.tests.test_screen_stream_diagnostics openpilot.system.ui.lib.tests.test_screen_stream_latency -v
```

FFmpegパスをSCREEN_STREAM_TEST_FFMPEGへ指定すると、不規則入力の統合テストを実行できる。GPUテストには追加でSCREEN_STREAM_TEST_GPU=1を指定する。Linux例:

```sh
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg python -m unittest openpilot.system.ui.lib.tests.test_screen_stream_timestamps -v
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg SCREEN_STREAM_TEST_GPU=1 python -m unittest openpilot.system.ui.lib.tests.test_screen_stream_video -v
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg python -m openpilot.system.ui.lib.tests.benchmark_screen_stream --frames 100 --output /tmp/stream-pipe.json
```

Windows PowerShell例（パスは使用するFFmpegに変更）:

```powershell
$env:SCREEN_STREAM_TEST_FFMPEG='C:\ffmpeg\bin\ffmpeg.exe'
$env:SCREEN_STREAM_TEST_GPU='1'
python -m unittest openpilot.system.ui.lib.tests.test_screen_stream_timestamps openpilot.system.ui.lib.tests.test_screen_stream_video -v
python -m openpilot.system.ui.lib.tests.benchmark_screen_stream --frames 100 --output stream-pipe.json
```

2026-09-22、Windows / Python 3.12 / FFmpeg 7.1 / Raylib 6.1-dev / RTX 3060で検証。既存96件相当を維持し、本番observer・I/O・Linux計測のモック検証を追加する。前回実行できなかったGPU統合も今回は3件実行できた。Linux select / ioctl / procはこの環境ではモックで確認し、commaでの実測は未実施。最終件数とbenchmark結果は下記に記録する。

既存の設定・翻訳・再起動・排他・送信エラー・75ms破棄・100ms書き込み期限・キャプチャ時刻・monitor検証を維持。188／376／564／1316 bytesの順序とTS境界、564の既定集約、SO_SNDBUF強制指定なし、getsockopt、outq 100ms制限とwindow reset、区間比率・rate、混雑警告でもPID維持、5秒未満のfinal flushと例外時cleanupを検証する。Linux select / ioctlはWindowsではモック。フレーム診断は分割PESヘッダー・PTS・識別子・欠落／順序変更拒否を検証し、実FFmpegと本番Python senderのローカルUDP経路でbytesが一致した。

### 各frameのstdin完了からstdout初観測まで

benchmark_screen_stream.pyは500／1000／1500／3000 kbit/sを順に測る。入力合成画像に16 bitのframe番号を白黒の帯で埋め、到着時刻・全stdin書き込み完了T1をperf_counterで記録する。stdoutでは各readの完了時刻と累積byte境界だけを記録し、読み取ったbytesを保持する。時間測定終了後にTSの映像PES PTSを解析し、そのPES先頭byteを含む最初のread時刻T2を割り当てる。[FFmpegのMPEG-TS muxer実装](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/mpegtsenc.c)のPTS形式に対応する。

さらにFFmpegで復号し、frame番号が入力順に0～N-1で一致すること、入力・PES・復号frame数が等しいこと、PESと復号後の相対PTSが一致することを検査する。欠落・重複・順序変更・不一致は計測失敗とし、単にN番目のreadをN番目のframeとは見なさない。対象は本番コマンドの単一映像PID・B=0・各frameのPESであり、一般のTS解析器ではない。

JSONのframe_timingsにinput_arrival_s、stdin_complete_s、pts_90k、stdout_first_observed_s、stdin_to_stdout_ms（T2−T1）、arrival_to_stdout_msを保存する。summaryは平均・p95・最大を含む。T2はPES先頭を読み取った観測時刻で、frame全体の出力完了・socket送信完了・受信時刻ではない。エンコーダ単体ではなく、mux・パイプ・OSスケジューリング・read単位の遅れも含む。writerがwriteから復帰する前にreaderが記録する場合は負値があり得るため、0へ丸めずnegative_latency_samplesも出す。

厳密照合のための全体保存・marker復号はこの合成映像benchmarkに限定する。オフライン解析と復号は計測後に実施する。同時に、本番の小さいobserverへ同じ入力・read時刻を渡して結果を比較する。Windowsのmonotonicは分解能が粗い場合があるためbenchmark内は高分解能perf_counterに統一し、本番Linuxではcaptureと同じmonotonicを使う。入力は同じ合成模様＋番号で、実UIの運動量・GPU負荷・最悪負荷を代表しない。

`4d032be`時点の開発PCのパイプ単独、100frame・20fps相当の実測（全条件で番号とPTSが一致、PTS経過4.950秒、負値0件）:

| kbit/s | stdin平均／最大ms | 各frame stdin→stdout 平均／p95／最大ms | 到着→stdout平均ms | stdout bytes/秒 |
| --- | --- | --- | --- | --- |
| 500 | 0.845 / 12.871 | 5.642 / 10.379 / 12.996 | 6.487 | 35,239 |
| 1000 | 1.249 / 13.506 | 8.789 / 10.110 / 11.301 | 10.038 | 97,699 |
| 1500 | 1.176 / 13.102 | 8.461 / 10.087 / 11.837 | 9.638 | 154,410 |
| 3000 | 0.866 / 12.969 | 6.183 / 10.948 / 12.090 | 7.048 | 309,705 |

初回stdoutは約21.7～22.2ms、全処理throughputは約20.07～20.09fps。パイプ単独はUDP・GPU・受信表示を含まない。設定bitrateと実際の出力bitrateも一致するとは限らない。この値からcomma実機の遅延や今回の改善量を推定しない。

| kbit/s | 実TS bytes | 188 / 376 / 564 / 1316でのdatagrams・sendto回数（TS長から計算） |
| --- | --- | --- |
| 500 | 175,404 | 933 / 467 / 311 / 134 |
| 1000 | 486,544 | 2588 / 1294 / 863 / 370 |
| 1500 | 769,108 | 4091 / 2046 / 1364 / 585 |
| 3000 | 1,543,480 | 8210 / 4105 / 2737 / 1173 |

### commaで逆圧を比較する診断

通常のUDP Screen StreamingをOFFにし、競合するFFmpegがない状態で実行する。`--frames 400`なら各bitrate約20秒。最初はパイプ単独、次に同じ条件で本番MpegTsUdpSenderを通す。送信元・宛先は実際のWi-Fi IPv4へ置換し、UDP時はPCでreceiverを1つ起動する。

```sh
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg python -m openpilot.system.ui.lib.tests.benchmark_screen_stream --frames 400 --output /tmp/stream-pipe.json
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg python -m openpilot.system.ui.lib.tests.benchmark_screen_stream --frames 400 --udp-address 192.168.4.44 --local-address 192.168.4.38 --port 12346 --output /tmp/stream-udp.json
```

FFmpegの実パスを指定する。UDP時もエンコーダのprotocol whitelistはfile,pipeのままで、FFmpegのUDP対応は不要。transport_statsは送信成功・drop率・outq・stdout量を含む全診断区間の集計であり、通常UIの5秒statsとは区別する。ローカル保存するTSはUDP送信前なので、送信dropがあってもローカルのframe対応検査は成功し得る。必ずtransport_statsとPC側の映像破損も確認する。

パイプ単独でもT2−T1が大きければencoder／mux／読み出しを調べる。UDP経由だけでstdinやT2−T1が悪化し、drop・outqも増えれば下流負荷の影響を疑う。各bitrateで繰り返し、温度とCPU負荷も揃える。合成映像は通常UIとは異なるため、診断だけで実機UIの因果を断定しない。

### 本番observerとの照合と計測負荷

CLIは本番observerを既定で有効にする。`--without-observer`を加えるとobserverだけ無効にできる。これはベンチマーク用の比較スイッチで、本番UIの新しい設定ではない。厳密なmarker/PES対応が成功したうえで、observerの有効状態、全入力と同数の確定標本、直近最大200件のstdin完了→PESの平均・p95・最大の差を検査する。1msを超える差は失敗扱いとする。パイプ単独ではUDP送信を模擬して内部記録を確定するが、送信時間の値は結果から除外し、UDPの測定値として報告しない。

本実装の開発PCで各100frameを計測した結果。observerの呼び出し時間はbegin・complete・TS解析・対応付けにかかった経過時間の合計をframe数で割ったもの。CPU使用率ではなく、senderのI/O計測・Linux ioctl・proc読取のコストは含まない。

| kbit/s | observer ONのstdin→PES 平均／p95／最大ms | observer OFFの平均ms（別試行） | observer呼出ms/frame | ONのthroughput fps |
| --- | --- | --- | --- | --- |
| 500 | 6.276 / 10.355 / 11.889 | 9.438 | 0.138 | 20.092 |
| 1000 | 7.225 / 10.002 / 10.660 | 9.033 | 0.209 | 20.172 |
| 1500 | 7.510 / 9.989 / 11.285 | 8.927 | 0.246 | 20.092 |
| 3000 | 9.813 / 11.228 / 12.090 | 9.778 | 0.434 | 20.074 |

全条件で100件の対応が確定し、diag_sync_lost=0。厳密診断との差は平均・p95・最大とも0.0005ms以内（統計の丸め差）。OFFのthroughputは20.076～20.084fps。別試行のOSスケジューリングや同時負荷の変動があるため、ONの方が速い条件を性能改善とは解釈しない。大きなthroughput低下は見られないが、comma上での全instrumentationの負荷増加を保証する結果ではない。

最終テストは121件中118件成功・GPU3件skip（最終実行セッションでは画面／OpenGL初期化不可）。それ以前の同じ作業中には119件全件成功し、GPU3件も実行済み。最終追加は時刻順序の不正検査と、診断喪失時に配信・PIDを維持する検証である。実FFmpegの4ビットレート照合と本番sender経由のローカルUDP照合は成功した。Ruffとgit diff --checkも通過。

追加検証はchunk境界、188／564／1316境界、B=0順序、完了前stdout、負の完了→PES、PTS逆行・jump・wrap、TS continuity、欠落時の保留、bounded queue・標本、window reset、restart時の初期化、datagram対応・drop、stdin/sendto/read計測、FIONREADとproc失敗、OFF／idle、finalへの新metrics、同期喪失時の配信継続を含む。

今回の本体ビルド・Linux実socket／proc計測・変更後の実機負荷／画面遅延・本家CI全体は未実施。Windowsの単体テストではswaglogのネイティブ依存をモックにし、cloudlogへ渡す内容を検証する。実機でcloudlogが保存されることは既報。

## 残る要因と次段階

100msには、20fps取得周期（1周期50ms）、end_drawingの表示待ち、Pythonコピー・スケジューリング、パイプとFFmpeg内部キュー、ソフトウェアx264、TS解析、無線再送、PC復号と表示同期が影響し得る。GPUは現状の実測では小さいため改修しない。今回は実UIのcaptureから先頭datagramのsendto復帰までを観測するが、frame全体や受信表示までの直接計測ではない。wallclock補正の不連続と20fps time baseの量子化も残る。

まず564 bytes・OS既定SO_SNDBUFでdrop・outq・stdin・queue・新stageを再計測する。diagが有効で送信drop・outq・pipe滞留が小さく、stdin→PESとPES→sendも小さいのに端末間遅延が数百ms残る場合、frame全体と受信側の残りを切り分けた上でRTP/H.264を比較する。FFmpeg CPUが高くstdin待ち・stdin→PESが大きい一方、pipe/outqが小さく、パイプ単独の厳密診断でも同傾向ならhardware encoderを検討する。CPUやstdin待ちの単独値だけで決めない。今回RTP・RTSP・WebRTC・SRT・hardware encoder・既存encoder流用・PBO・DMA-BUF・zero-copy・fps変更は実装しない。

[本家の開発ガイド](CONTRIBUTING.md)と[AI支援方針](AI_POLICY.md)に従い、コミットにAssisted-byを記載する。
