# UDP画面配信: ultra low latency experimental

## 目的と実機状況

comma 3X → Windows PCのunicastを基本候補とし、glass-to-glass 100ms以下を目標に計測と調整を行う実験版。100ms達成を保証するものではなく、本変更後の実機遅延は未測定。MIB・MOST・AIDは変更しない。H.264 / MPEG-TS / UDPを維持し、multicastも残す。

利用者による`40e0a4961`の実機報告:

- comma `192.168.4.38` → PC `192.168.4.44:12346` のunicastで画質が大幅改善。従来のmulticastではPacket corrupt・H.264 macroblockエラーが多かった。
- FFmpeg PIDは安定。以前のsender集計は`sent=21536 dropped=0`。
- 遅延は数百ms～約1000ms。1500 kbit/sは500 kbit/sより遅い。
- 両ビットレートでqdiscのdropped / overlimits / requeues / backlogは0。
- Windows FFplayの`-avioflags direct`で “Part of datagram lost due to insufficient buffer size” と映像破損が多発。このオプションは推奨から除外する。

qdiscやPythonの破棄数が0でも、エンコーダ・socket・無線ドライバー・受信側に待ちがないとは断定しない。

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

設定 → トグル → UDP Screen Streaming（日本語: UDP画面配信）をONにする。Destination AddressをPCのWi-Fi IPv4、ポート12346、最初は500 kbit/sに設定。受信できたら1500へ変更して同じ試験を行う。保存直後のsenderとFFmpegの再生成は正常であり、試験途中の障害再起動と区別する。

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

| 項目 | 従来 | 今回 |
| --- | --- | --- |
| 入力PTS | 受信フレーム数 / 20 | 入力wallclock |
| x264 preset | veryfast | ultrafast |
| x264 params | repeat-headers=1 | sync-lookahead=0:rc-lookahead=0:sliced-threads=1:repeat-headers=1 |
| VBV bufsize | bitrate / 3 | bitrate / 10 |
| FRAME_MAX_AGE | 250ms | 75ms |
| PIPE_WRITE_TIMEOUT | 500ms | 100ms |
| スケジューラ | SCHED_OTHER、nice +10 | SCHED_OTHER、niceを変更しない |

維持: 800×480、最大20fps、libx264、zerolatency、baseline、yuv420p、B=0、GOP=10、keyint_min=10、sc_threshold=0、threads=2、muxdelay=0、muxpreload=0、flush_packets=1、MPEG-TSのpipe:1出力。500 / 1500 / 3000 kbit/sのVBVは50 / 150 / 300 kbitで、開発PCのlibx264ではエラーや最小値へのclamp警告は出なかった。VBVはrate-controlの容量であり、その値を実際の100ms待機と解釈しない。

75ms以上古い未送信frameは、FFmpegへ入れる前に丸ごと破棄する。rawvideoを途中まで書いた後に100msの期限へ達した場合は、境界を壊さないようFFmpegごと再生成する。2つの上限は独立し、合計100ms以下を保証しない。以前の実機では最大114.2msのstdin書き込み報告もあるため、今回の厳しい期限で再起動が増えないか確認する。

配信ワーカーでSCHED_OTHERへ変更してからmonitor・sender・FFmpegを起動する。意図的なnice +10を廃止し、nice 0への昇格操作も行わない。親プロセスのniceを継承するため、通常priorityで起動していることは実機で確認する。CAP_SYS_NICEを必要とするpriority上昇やrealtime化は行わない。

## UDP送信とsocket計測

送信先の`IPv4Address.is_multicast`で判定する。両方式で非ブロッキングsocketとsendtoを使用し、connect / SO_BINDTODEVICE / アプリ側の固定bitrate pacingは追加しない。

| 項目 | unicast | multicast |
| --- | --- | --- |
| 送信元 | bind((Wi-Fi IPv4, 0)) | IP_MULTICAST_IF=Wi-Fi IPv4 |
| TTL | OS既定値、UI設定は無視 | IP_MULTICAST_TTL=保存値 |
| 通常UDP payload | 188 bytes（TS 1個） | 1316 bytes（TS 7個） |
| SO_SNDBUF要求 | 16 KiB | 明示変更なし |

読み取り境界とTS境界が異なっても順序どおり保持する。unicastはTS 1個が揃えば送信し、multicastは従来どおり7個へ集約する。正常EOFだけ残った完全なTSを送信し、停止時の末尾や不完全なTSは破棄する。TS途中で任意に間引く処理は追加しない。一時的なEAGAIN / ENOBUFS等ではデータグラムだけを破棄してFFmpegを維持し、致命的障害は再生成する。

Linuxでは非ブロッキングstdoutのEAGAIN後にselectで可読通知を待つ。50msは停止確認のための最大待機で、データ到着時は即時に起きる。Windowsの匿名パイプはselect非対応なので、開発PCでは1ms待機を使う。

SO_SNDBUFはgetsockoptで実値を読み、起動・統計ログへ出す。Linuxでは要求値の倍などになる場合があるため、要求16 KiBを実値と混同しない。[socket(7)](https://man7.org/linux/man-pages/man7/socket.7.html)

LinuxのTIOCOUTQでsocketのpending output bytesを、stdout読み取りバッチの送信後と統計出力時にサンプリングする。取得失敗は配信障害にせずcurrent=Noneとする。peakはsender生成以降のサンプル最大値で、未観測の瞬間ピークや無線ドライバー・AP・受信側のキューは含まない。[udp(7)](https://man7.org/linux/man-pages/man7/udp.7.html)

1500 kbit/sを188-byte payloadだけで換算すると約997 datagrams/s（TS・ネットワークの追加分は別）となり、1316-byte方式の約7倍。CPU負荷と無線効率は実機確認が必要。破棄が増えた場合はSO_SNDBUF_REQUESTを32 KiB、またはUNICAST_TS_PACKETS_PER_DATAGRAMを2（376 bytes）として、変更を1項目ずつ比較できる。

調整用定数は`openpilot/system/ui/lib/screen_stream.py`先頭にまとめる: FRAME_MAX_AGE、PIPE_WRITE_TIMEOUT、UNICAST_TS_PACKETS_PER_DATAGRAM、MULTICAST_TS_PACKETS_PER_DATAGRAM、SO_SNDBUF_REQUEST、LATENCY_STATS_INTERVAL、ENCODER_PRESET。環境変数による暗黙のoverrideは追加しない。

## 監視とキャプチャ

`screen_stream_monitor.py`のCachedQueryをネットワークと設定に1つずつ使用する。照会時はロックを持たず、結果・最後の正常値・error・generation・連続失敗数を短いロックで更新する。配信ワーカーはsnapshotだけを読み、D-Busと設定全体の読み取りを行わない。配信ON/OFFのBOOL確認はワーカーで継続する。

ネットワーク照会は接続中5秒・未接続時1秒、設定確認は1秒。D-Bus各リクエストの250ms上限、last-known-good、一時失敗時のPID維持、成功したNoneによる停止、tuple変更による再生成、警告の10秒間隔を維持する。OFF・消灯では照会を休止し、進行中の古い照会結果はepochで排除する。終了時はdaemonへ停止通知し、各200msまでjoinする。ブロック中のD-Bus呼び出し自体は強制中断せず、戻った後に結果を破棄して終了する。

`ScreenStreamCapture.capture()`はGPU操作前のmonotonic時刻を記録し、submit(data, captured=...)へ渡す。GPU縮小、同期load_image_from_texture、bytesコピーを区別して計測する。queue_ageはキャプチャ開始から取り出しまでなのでreadback時間も含む。frame_age_writtenはstdinへの全書き込み完了までを含む。

GPU readback remains synchronous。PBOは未実装。現行pyrayのload_image_from_texture / rl_read_texture_pixels経路と描画ループを確認したが、PBO専用APIとfence・複数フレームのリソース管理を新規に持ち込まず、まず計測を優先する。GPU描画命令の非同期実行による待ちはreadback側の時間へ現れることがある。

キャプチャ位置は`application.py`のend_drawing後を維持する。RenderTextureはend_texture_mode直後に完成するが、そこへ同期readbackを移すとローカル画面表示を遅らせる可能性がある。効果を実機で確認できないため移動せず、描画途中のtextureは読まない。描画完了からキャプチャ開始までの表示待ち時間は今回のcapture統計には含まない。

## 実機での再起動診断と遅延統計

```sh
grep -R -a "screen stream latency stats" /data/log 2>/dev/null | tail -30
grep -R -a "screen stream UDP drops" /data/log 2>/dev/null | tail -30
grep -R -a "screen stream restart" /data/log 2>/dev/null | tail -30
grep -R -a "screen stream started" /data/log 2>/dev/null | tail -10
pgrep -a -x ffmpeg
```

起動ログ: PID、mode、Wi-Fi tuple、destination、bitrate、TTL、socket_sndbuf実値、payload_size、preset。statsは配信ワーカーから約5秒ごとにcloudlogへ出し、毎frameのログは行わない。OFF・消灯・待機中は定期ログを出さない。

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
| frame_age_written_avg_ms / max_ms | キャプチャ開始から完全書き込み終了まで |
| sender_datagrams / sender_drops / sender_bytes | senderの累積送信成功数・破棄数・成功bytes |
| socket_sndbuf | getsockoptで取得した実容量 |
| socket_outq_current / peak | 最新サンプル／sender生成後のサンプル最大値。未対応・取得失敗はNone |
| pid / bitrate / mode | 現在のエンコーダと送信設定 |

capture～frames_writtenと時間統計は前回ログからの区間値で、各時間の平均はその測定サンプル数を分母にする。sender統計はsender生成からの累積値なので、同じPIDの連続ログの差分で比較する。エンコーダ再生成で区間統計とsender累積値はリセットされる。処理段階の境界をまたぐframeがあるため、同一区間の各countが必ず一致するわけではない。終了・再生成直前の5秒未満の区間統計はログに残らない場合がある。

500→1500でqueue_replaced増加・frames_written減少・stdin_write増加が同時に見えれば入力処理能力不足を疑う。capture/readbackが継続して10～20msを超えるならGPU同期の改善を検討する。outqが増えるならsocketより下流も調べ、sender_drops増加時は送信バッファ制限と小さいpacketの影響を比較する。これらが小さくてもPC表示が遅ければ、受信demux・decode・表示同期を調べる。

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
gst-launch-1.0 -v udpsrc address=0.0.0.0 port=12346 buffer-size=65536 caps="video/mpegts,systemstream=(boolean)true,packetsize=(int)188" "!" tsdemux latency=0 "!" h264parse "!" avdec_h264 "!" queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "!" videoconvert "!" autovideosink sync=false async=false
```

[tsdemux](https://gstreamer.freedesktop.org/documentation/mpegtsdemux/tsdemux.html)のlatency既定値は700msのため0を明示する。[queue](https://gstreamer.freedesktop.org/documentation/coreelements/queue.html)は復号後に置き、古い復号済みframeを落とす。圧縮H.264やTSの途中をqueueで間引かない。udpsrcの64 KiBは受信バッファ要求値で、実機のlossと併せて確認する。sync=falseは時計待ちを避けるが、どのreceiverでも100ms達成を保証しない。開発環境ではGStreamerと実機画面表示を実行していない。

## 実機A/Bと遅延測定

同じAP、機器位置、UI操作、PC電源設定、画面リフレッシュレートを固定する。各条件で10秒ウォームアップ後に5分間測定し、3回以上繰り返す。まず500、次に1500 kbit/sを比較する。通常の設定変更時だけPIDが変わり、測定中は安定していることを確認する。

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

| 条件 | 遅延中央値／最大ms | Packet corrupt / H.264エラー | queue replaced / written | readback / stdin最大ms | outq peak / drops | PID安定 |
| --- | --- | --- | --- | --- | --- | --- |
| Unicast 500 / FFplay通常 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未確認 |
| Unicast 1500 / FFplay通常 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未確認 |
| Unicast 500 / FFplay低遅延 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未確認 |
| Unicast 1500 / FFplay低遅延 | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未確認 |
| Unicast 500 / GStreamer | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未確認 |
| Unicast 1500 / GStreamer | 未測定 | 未測定 | 未測定 | 未測定 | 未測定 | 未確認 |

負荷・温度・UI FPS、OFF/ON、消灯/復帰、Wi-Fi切断/再接続/IP変更、録画との排他も確認する。実機の受信PTSを調べる場合は、例えば短時間だけPCで`ffmpeg -i "udp://0.0.0.0:12346" -t 10 -c copy sample.ts`と記録し、`ffprobe -select_streams v:0 -show_frames -show_entries frame=pts_time -of csv sample.ts`でgapを確認する。採取中は別receiverを同時起動しない。

## 自動テストとベンチマーク

通常の単体テスト:

```sh
python -m unittest openpilot.system.ui.lib.tests.test_screen_stream openpilot.system.ui.lib.tests.test_screen_capture openpilot.system.ui.lib.tests.test_screen_stream_settings -v
```

FFmpegパスをSCREEN_STREAM_TEST_FFMPEGへ指定すると、不規則入力の統合テストを実行できる。GPUテストには追加でSCREEN_STREAM_TEST_GPU=1を指定する。Linux例:

```sh
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg python -m unittest openpilot.system.ui.lib.tests.test_screen_stream_timestamps -v
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg SCREEN_STREAM_TEST_GPU=1 python -m unittest openpilot.system.ui.lib.tests.test_screen_stream_video -v
SCREEN_STREAM_TEST_FFMPEG=/usr/bin/ffmpeg python -m openpilot.system.ui.lib.tests.benchmark_screen_stream --frames 100
```

Windows PowerShell例（パスは使用するFFmpegに変更）:

```powershell
$env:SCREEN_STREAM_TEST_FFMPEG='C:\ffmpeg\bin\ffmpeg.exe'
$env:SCREEN_STREAM_TEST_GPU='1'
python -m unittest openpilot.system.ui.lib.tests.test_screen_stream_timestamps openpilot.system.ui.lib.tests.test_screen_stream_video -v
python -m openpilot.system.ui.lib.tests.benchmark_screen_stream --frames 100
```

2026-09-22、Windows / Python 3.12 / FFmpeg 7.1 / Raylib 6.1-dev / RTX 3060で検証。単体78件、GPU・実UDP統合3件、不規則PTS統合2件（bitrate別subtestを含む）。Ruffとgit diff --checkも実行する。

既存の設定・翻訳・再起動・排他・送信エラー検証を維持し、188/1316 bytesの順序・完全性、単一TSの即送信、SO_SNDBUF実値、outq失敗の非致命性、75ms破棄、100ms書き込み期限、readback前の時刻、区間統計、250ms以上ブロックするnetwork/config照会中のフレーム書き込み、監視休止時の古い結果排除を追加した。Linuxのselect / ioctlはWindows上ではモックによる検証。GPU統合は実際のstdout→Python socket→UDP→復号で両方式を確認する。

開発PCの100frame・20fps相当・同一合成模様の計測:

| bitrate kbit/s | stdin平均／最大ms | 初回stdout ms | throughput fps | 入力経過／PTS経過s |
| --- | --- | --- | --- | --- |
| 500 | 0.95 / 13.23 | 22.19 | 20.08 | 4.949 / 4.950 |
| 1500 | 1.03 / 13.91 | 26.64 | 20.07 | 4.950 / 4.950 |
| 3000 | 0.99 / 13.59 | 21.79 | 20.07 | 4.950 / 4.950 |

時間はperf_counterで計測。初回stdoutはプロセス生成直後から最初のMPEG-TS bytesまでで、各frameのエンコード遅延やglass-to-glassではない。throughputは入力間隔を含む全処理時間の平均。合成模様は各frame同じため、実UIの運動量や最悪負荷を代表しない。ベンチマークはUDP・GPU・受信表示を含まない。数値をcomma実機へ外挿しない。

本体ビルド・Linux実socket計測・実機負荷・GStreamer受信・本家CI全体は未実施。Windowsの単体テストではswaglogのネイティブ依存をモックにし、cloudlogへ渡す内容を検証する。実機でcloudlogが保存されることは既報。

## 残る要因と次段階

100msには、20fps取得周期、end_drawingの表示待ち、同期GPU readback、Pythonコピー・スケジューリング、パイプとFFmpeg内部キュー、ソフトウェアx264、TS解析、無線再送、PC復号と表示同期が影響し得る。今回のstatsはFFmpeg内部の全frame待ち時間を直接測定しない。wallclock補正の不連続と20fps time baseの量子化も残る。

まず今回の統計とreceiver比較で支配的な区間を特定する。GPUが支配的ならPBO、エンコードが支配的なら実機で存在確認したhardware encoderを検討する。MPEG-TS/receiver側が支配的で100msを切れない場合には、次段階としてAnnex-B → RTP/H.264 → UDP unicastを比較する。RTP化は今回未実装で、現時点で必須とは判断しない。

[本家の開発ガイド](CONTRIBUTING.md)と[AI支援方針](AI_POLICY.md)に従い、コミットにAssisted-byを記載する。
