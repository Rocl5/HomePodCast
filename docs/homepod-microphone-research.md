# HomePod microphone uplink research / HomePod 麦克风上行研究

## Status — 2026-11-14

**This fork does not record the HomePod microphone.** This is a feasibility
investigation for a second-generation HomePod, not a working recording feature.
Read-only metadata queries and transient HAP authentication succeeded against one
physical HomePod. Experimental input-stream negotiation has been attempted, but
no microphone stream has been confirmed and no microphone audio received.
The standalone prototype can record synthetic negotiated PCM in offline tests;
it is not a working microphone feature in the Windows app.

Baseline: upstream `qiyinxi/HomePodCast`, commit
`9dd5b63ca6ca59448c922b25ba09354f8dab37d0`.

Source inspection establishes:

- `src/Audio/MicCapture.cs` captures a **Windows WASAPI input endpoint**. It does
  not fetch the HomePod microphone and explicitly does not retain recordings.
- `src/Net/AirPlayClient.cs` negotiates realtime stream type 96 and constructs
  an `RtpSender` directed at the speaker's returned data/control ports.
- Its `Record()` sends RTSP `RECORD` to start that playback stream. The name does
  not identify an API for opening or receiving a microphone.
- No implemented HomePod microphone uplink has been identified in this baseline.
  This finding does not prove that all private Apple protocols lack such a path.

Protocol references:

- <https://openairplay.github.io/airplay-spec/audio/rtsp_requests/record.html>
- <https://openairplay.github.io/airplay-spec/audio/rtsp_requests/setup.html>
- <https://openairplay.github.io/airplay-spec/features.html>

## Implemented: metadata probe only

`tools/homepod_mic_probe.py` uses Python 3's standard library. It requires no .NET
SDK and makes only `GET /info` and `OPTIONS *` requests to an explicitly supplied
IP and port. It does not scan the network, pair, set up a session, take over
playback, change volume, open a local listening port, or access a microphone.

From the fork's root, substitute your HomePod's actual LAN IP:

```powershell
python tools/homepod_mic_probe.py --host <HOMEPOD_IP> --output mic-report.json
```

The AirPlay control port defaults to 7000; use `--port` if the discovered service
advertises another port. The timeout defaults to 5 seconds per connection/read
operation, and can be changed with `--timeout` (maximum 30 seconds). Two requests
are made, with independent connections.

Inspect an existing raw binary/XML `/info` property list without any networking:

```powershell
python tools/homepod_mic_probe.py --info-file info.plist --output mic-report.json
```

The report selects model, software/protocol version, feature bit numbers, and
paths of input-related keys; it omits device name, device ID, pairing keys, and
unknown field values. Review any diagnostic report before sharing it. An
existing output file is never overwritten. No audio file is produced.

Exit 0 means metadata was read, **not that microphone capture works**. Exit 2
means an operational/parse failure or unavailable info (including authentication
required); a JSON report is still emitted when probing can produce one.
Missing input fields, an advertised input field, HTTP/RTSP 401/403/470, and
`RECORD` listed in `OPTIONS` are all **inconclusive** about microphone access.

Offline tests, including an ephemeral loopback-only mock receiver:

```powershell
python -B -m unittest discover -s tools/tests -p test_homepod_mic_probe.py -v
```

## Milestones toward a working implementation

1. **Done:** confirm the HomePod's IP, advertised AirPlay port and audioOS build,
   and inspect real metadata. Fixture data was not substituted for hardware.
2. **Not solved:** identify an authenticated microphone session and its transport. If this
   requires reproducing Apple's call handoff, investigate that separately from
   the existing playback sender; do not invent an endpoint or capability bit.
3. Demonstrate actual HomePod-originated audio: controlled speech, tap tests and
   Windows-input isolation must distinguish it from a PC mic or playback echo.
4. **Partly implemented offline:** PCM decoding, WAV recording and clean stop;
   these must be connected to a verified input session. Jitter buffering remains.
   Using it in other Windows apps additionally needs a virtual input endpoint;
   writing a WAV file alone does not create a Windows microphone device.

No completion date or recording support is claimed until step 3 passes.

## Implemented: experimental input-session and recording prototype

`tools/homepod_mic_capture.py` implements transient HAP authentication, encrypted
control, NTP timing, the encrypted event channel, input SETUP hypotheses, RTP
parsing, PCM16 WAV writing and session teardown. It never opens a Windows audio
input. It checks the exact device model and idle status before authentication
and again before session SETUP. It makes no volume changes or outbound audio.

Create a separate Python environment; no system install or PATH change is needed:

```powershell
python -m venv .git/mic-venv
.git/mic-venv/Scripts/python.exe -m pip install -r tools/requirements-mic-research.txt
.git/mic-venv/Scripts/python.exe tools/homepod_mic_capture.py --host <HOMEPOD_IP> --profile main-telephony --output .git/input-attempt.wav --report .git/input-attempt.json --seconds 10
```

Use fresh output/report paths for every run. The profiles are **hypotheses**:
`main-telephony` (100/telephony), `main-speech` (100/speechRecognition), and
`realtime-telephony` (96/telephony). All explicitly request `input=true` and PCM
16-bit mono at 16 kHz. MainAudio input is described for CarPlay, not established
for HomePod. References:

- <https://github.com/maxim-smirnov/mhi2-carplay-cluster/blob/main/docs/audio/CALL_AND_SIRI_ROUTING.md>
- <https://github.com/postlund/pyatv/blob/master/pyatv/protocols/airplay/auth/hap_transient.py>

The recording path requires the returned stream to explicitly confirm direction
and PCM format. A missing echo is **inconclusive**, not proof of no microphone.
For a returned stream lacking direction/format confirmation, observation mode can
start the session and count candidate RTP without saving or decoding its payload:

```powershell
.git/mic-venv/Scripts/python.exe tools/homepod_mic_capture.py --host <HOMEPOD_IP> --profile realtime-telephony --observe-only --report .git/input-observation.json --seconds 10
```

Observation cannot bypass a rejected SETUP, a missing stream or the wrong stream
type. Neither mode claims that observed packets come from a microphone. The
report always leaves `homepod_microphone_verified=false`. Exit 0 means synthetic
or real negotiated audio was saved, or candidate RTP was observed in observation
mode; it never certifies microphone support. Exit 2 reports a failed experiment.

No WAV is created for failed negotiation or a no-audio timeout. For supported
PCM only, valid packets from the selected peer create an exclusive WAV. It
handles RTP extensions, padding, duplicates and sequence/timestamp wrap, stops
on loss/discontinuity instead of inserting fabricated samples, and finalizes any
partial WAV on cancellation. This first prototype has no jitter reordering,
encrypted audio decoder, AAC/Opus decoder, GUI integration or virtual Windows
microphone endpoint.

The observer accepts structurally valid candidate RTP with different payload
types, including 96 and 100; it does not assume that every stream uses 96.
It rejects malformed packets and multiplexed RTCP. The PCM decoder separately
requires the profile's payload type (100 for the MainAudio hypotheses; 96 for
realtime). Observation never treats an unknown codec as PCM. Reports include
target data-port datagram/byte counts, candidate payload-type counts and timing
request/reply counts. A timing reply is a positive control for that UDP socket,
not proof that every audio port or route is reachable.

Tests remain offline/loopback-only:

```powershell
python -B -m unittest discover -s tools/tests -p 'test_homepod_mic*.py' -v
```

## Hardware observation — 2026-11-14

After the PC joined the speaker's Wi-Fi network, mDNS discovery identified
`AudioAccessory6,1` with software version `27.2` and AirPlay version `1005.13.2`.
Both IPv4 and IPv6 `GET /info` and `OPTIONS *` returned status 200. `/info`
confirmed build `24K122`. No pairing, playback session, or volume change was made.

The info dictionary had no `audioFormats`, `audioLatencies`, or explicit
microphone/audio-input keys. It did have `supportedFormats`,
`supportedAudioFormatsExtended`, and playback capabilities. These observations
are not evidence that every private microphone path is unavailable. `OPTIONS`
listed `RECORD`; this still refers to the existing streaming protocol and does
not establish microphone capture support.

The actual device name, addresses, identifiers, raw plist, and network discovery
records are not committed. Local diagnostic reports remain under `.git/`.
The existing Windows capture implementation has not been changed or tested as
part of this research. The new backend is an isolated Python experiment.

Authenticated follow-up, on the same device/build:

| Hypothesis | Session SETUP | Input SETUP | Result |
| --- | --- | --- | --- |
| Main telephony (100) | 200 | 200, empty body | No input stream allocated; no WAV |
| Main speech (100) | 200 | 200, empty body | No input stream allocated; no WAV |
| Realtime telephony (96) | 200 | 200, stream type 96 only | Input/format not confirmed; no WAV |
| Realtime observation (96) | 200 | 400 on a later attempt | Rejected before observation; no WAV |

Transient authentication and encrypted `/info` worked. All established sessions
were torn down with status 200. A preliminary type-100 experiment sent RECORD
after the empty SETUP reply and observed no packets for three seconds; the
implemented recording path now rejects that empty reply before RECORD. These
results describe these specific requests only, not every Apple call protocol.
To proceed beyond these hypotheses, the missing artifact is a known-working
HomePod microphone/call negotiation trace or equivalent protocol implementation.

### Further physical-device tests after playback was disconnected

At 15:46–15:47 (UTC+8), the corrected observer was tested with two additional
request shapes. These were diagnostic hypotheses, not supported recording
profiles. Each sent `input=true`; both observed for 12 seconds after RECORD.

| Request | Input SETUP | RECORD / teardown | Target data-port packets | Timing requests / replies |
| --- | --- | --- | --- | --- |
| Realtime 96, speechRecognition, PCM 44.1 kHz stereo | 200, stream 96 without input/format confirmation | 200 / 200 | 0 | 8 / 8 |
| Main 100, telephony, PCM 44.1 kHz stereo | 200, empty body | 200 / 200 | 0 | 7 / 7 |

The second row used a separate diagnostic harness to observe after an empty
SETUP, without relaxing the production recording guard. No payload was decoded
or WAV created. Earlier bounded trials also tried 48 kHz PCM and AAC-ELD request
shapes; none produced inbound audio. Some type-96 trials returned 400, so these
experiments do not establish a complete format compatibility matrix.

A separate transient-authenticated AirPlay data stream (type 130) successfully
exchanged encrypted MediaRemote device information. The receiver identified
`AudioAccessory6,1` and advertised last message type 139. Two bounded
`GET_VOICE_INPUT_DEVICES` queries, including the explicit empty protobuf extension
31 and observing responses without identifiers, received no voice-device reply.
A subsequent generic heartbeat **did** receive its matching response. This
distinguishes a functioning control transport from an unanswered voice query;
it does not establish that this MediaRemote message can expose physical HomePod
microphones. No recording-state or voice-upload command was sent.

Relevant implementation and schema references:

- <https://github.com/postlund/pyatv/blob/v0.18.0/pyatv/protocols/airplay/ap2_session.py>
- <https://github.com/postlund/pyatv/blob/v0.18.0/pyatv/protocols/airplay/mrp_connection.py>
- <https://github.com/JarekToro/bunatv/blob/main/src/protocols/mrp/protobuf/messages/audio/GetVoiceInputDevicesMessage.proto>
- <https://www.rfc-editor.org/rfc/rfc5761#section-4>

These additional tests still leave the microphone milestone unsolved. They
provide no measured microphone latency, no usable HomePod recording and no
Windows input device. The 67 passing offline tests verify software behavior with
synthetic packets only.

## 当前状态 — 2026-11-14

**这个 fork 尚不能录制 HomePod 自带麦克风。** 当前是二代 HomePod 的可行性研究，
已成功读取真实 HomePod 的设备信息并完成临时 HAP 鉴权，已尝试输入流协商，
但没有确认任何麦克风流，也没有录到 HomePod 麦克风音频。独立录音原型可以在
离线测试中保存模拟协商后的 PCM；Windows 应用尚未获得可用的 HomePod 录音功能。
基于上方固定提交，检查结果为：

- `MicCapture.cs` 采集的是 Windows 的 WASAPI 录音设备，不是 HomePod 麦克风；
  原实现也不保存录音。
- `AirPlayClient.cs` 协商 type 96 音频流，再由 `RtpSender` 把数据发往音箱。
- `Record()` 的 RTSP `RECORD` 命令启动该播放流，不能据此认为已有麦克风入口。
- 在这个版本里没有找到已实现的 HomePod 麦克风上行通道；这不等于证明苹果私有协议
  绝对不存在相应能力。协议参考见上方链接。

### 已实现：仅设备信息探测

`tools/homepod_mic_probe.py` 只依赖 Python 3 标准库，无需 .NET SDK。
使用上方 PowerShell 命令，将 `<HOMEPOD_IP>` 换成真实 IP。它只发送 `GET /info`
和 `OPTIONS *`，不扫描网段、不配对、不建立播放会话、不抢占音箱、不改音量、
不打开本地监听端口，也不读取任何麦克风。还可用 `--info-file` 离线检查原始 plist。

端口默认 7000，可用 `--port` 指定实际广播的 AirPlay 端口；每次连接和读取操作默认
超时 5 秒，`--timeout` 最大 30 秒；两个请求各自建立连接。

JSON 报告只选取型号、版本、能力位编号及音频输入相关键的路径，不导出设备名称、
设备 ID、配对密钥及未知字段值。分享前仍需自行检查。不覆盖已有输出文件，不生成
录音。退出码 0 仅代表读到设备信息；退出码 2 代表操作失败、解析失败或信息不可用
（包括需要鉴权），能够生成报告时仍输出 JSON。

没有音频输入字段、有音频输入字段、返回 401/403/470、`OPTIONS` 列出了 `RECORD`，
都不能单独证明能否使用 HomePod 麦克风。报告始终将麦克风录音能力标为 `unverified`。
上方测试命令只使用离线数据和本机回环模拟服务，不连接真实音箱。

### 实现进度及剩余问题

1. 已完成：确认 IP、AirPlay 端口及 audioOS 版本，已取得真实设备信息，未用模拟数据代替。
2. 尚未解决：找到并验证经过鉴权的麦克风会话和音频传输通道；若依赖苹果通话接力协议，需要
   单独研究，不能凭空假设一个接口或能力位。
3. 通过说话、敲击及隔离电脑输入设备，确认收到的确实是 HomePod 麦克风，而不是
   电脑麦克风或播放回声。
4. 部分代码已通过离线测试：PCM 解码、WAV 保存和正常停止；仍需接入已验证的输入
   会话，抖动缓冲尚未实现。若要让其他 Windows 软件选择它作为
   麦克风，还需要虚拟录音端点；保存 WAV 并不会自动创建 Windows 输入设备。

在第 3 步通过前，不承诺录音成功或交付日期。

### 已实现：实验性输入会话及录音原型

`tools/homepod_mic_capture.py` 实现了临时 HAP 鉴权、加密控制连接、NTP 时钟响应、
加密事件通道、输入流协商尝试、RTP 解析、PCM16 WAV 保存及会话清理。它不读取
Windows 麦克风，在鉴权前和建立会话前检查目标型号及空闲状态，不改音量，不向音箱
发送音频。依赖安装和运行命令见上方；使用 `.git/mic-venv` 独立环境，每次使用新的
WAV 和 JSON 文件路径，不需要系统安装或修改 PATH。

三个配置都是协议假设：`main-telephony`、`main-speech`、`realtime-telephony`。
均明确请求 `input=true`、16 kHz 单声道 PCM16；前两者借鉴 CarPlay 的 MainAudio
输入，不能据此假定 HomePod 支持。参考链接见英文段落。

正常录音要求回复明确确认输入方向和 PCM 格式；字段缺失不等于证明设备不支持。
`--observe-only` 可在收到对应流但未确认输入/格式时启动并只统计候选 RTP，不保存、
不解码负载；不能绕过 SETUP 拒绝、没有流或流类型不符。两种模式都不能仅凭数据包
证明是麦克风，报告始终保留 `homepod_microphone_verified=false`。

退出码 0 只表示保存了协商格式的音频或观察到了候选 RTP，并不认证 HomePod 录音
可用；退出码 2 表示实验失败。协商失败或没有音频时不创建 WAV；支持的 PCM 流只有
收到目标设备有效数据后才创建文件。支持 RTP 扩展、填充、重复包、序号/时间戳回绕；
遇到丢包/不连续则停止并保留部分录音，不伪造静音；取消时修正 WAV 文件头。
当前没有抖动重排、加密音频/AAC/Opus 解码、GUI 集成或 Windows 虚拟麦克风端点。
全部离线测试命令见上方，不连接真实音箱。

观察器不再把所有流都假定为 RTP 类型 96，可统计包括 96、100 在内的不同候选类型，
并拒绝结构损坏的数据包及复用的 RTCP。PCM 解码仍单独校验配置指定的类型：MainAudio
假设使用 100，实时流使用 96，不将未知编码当作 PCM。报告增加目标数据端口的包数、
字节数、候选 RTP 类型以及时钟请求/回复计数。收到时钟请求只能确认对应 UDP 路径，
不能据此断言所有音频端口或传输路径均畅通。

### 真机观察 — 2026-11-14

电脑连接音箱所在 Wi-Fi 后，通过 mDNS 找到型号 `AudioAccessory6,1`，广播软件版本
`27.2`、AirPlay 版本 `1005.13.2`。IPv4 和 IPv6 的 `GET /info`、`OPTIONS *`
都返回 200，设备信息确认构建号 `24K122`。没有执行配对、建立播放会话或调整音量。

本次设备信息没有 `audioFormats`、`audioLatencies` 或明确的麦克风输入字段，
有 `supportedFormats`、`supportedAudioFormatsExtended` 及播放能力字段。这不足以
排除所有私有麦克风通道。`OPTIONS` 列出了 `RECORD`，仍不能作为支持录音的证据。

设备名称、地址、标识符、原始 plist 和网络发现记录不提交到仓库；本地诊断报告保存在
`.git/` 内。原有 Windows 采集实现没有修改或测试，新增后端是独立 Python 实验。

同一设备/构建的鉴权后测试：两条 type 100 输入请求都返回 200 但正文为空，没有
分配输入流；type 96 请求曾返回 200 及流类型 96，但没有确认输入/格式，后续观察
尝试则收到 400，均未创建 WAV。临时鉴权和加密 `/info` 成功，已建立会话的清理
都返回 200。初步 type 100 实验在空回复后发送过 RECORD，监听三秒没有数据包；
现有录音路径在空回复后会直接停止，不发送 RECORD。

以上只描述这些具体请求，不能排除所有苹果通话协议。继续打通所缺的是已知可工作的
HomePod 麦克风/通话协商抓包或等价协议实现。

### 断开播放后的进一步真机测试

15:46–15:47（UTC+8），修正后的观察器再次测试了两种诊断请求。它们都明确发送
`input=true`，在 RECORD 后监听 12 秒，但并非已获支持的录音配置。

| 请求 | 输入 SETUP | RECORD / 清理 | 目标音频端口数据包 | 时钟请求 / 回复 |
| --- | --- | --- | --- | --- |
| 实时 96，speechRecognition，44.1 kHz 双声道 PCM | 200，仅返回流 96，未确认输入/格式 | 200 / 200 | 0 | 8 / 8 |
| Main 100，telephony，44.1 kHz 双声道 PCM | 200，正文为空 | 200 / 200 | 0 | 7 / 7 |

第二项用独立诊断脚本在空 SETUP 回复后继续观察，没有放宽正式录音路径的保护条件。
两项均未解码负载、未创建 WAV。此前还做过有时限的 48 kHz PCM、AAC-ELD 请求测试，
也没有收到音频；部分实时流请求返回 400，不能把这些测试当作完整的格式兼容性表。

另一条临时鉴权后的 AirPlay 数据通道（type 130）成功交换了加密的 MediaRemote
设备信息。回复确认型号 `AudioAccessory6,1`、最大消息类型 139。两种
`GET_VOICE_INPUT_DEVICES` 查询都明确包含空 protobuf 扩展 31，并检查了没有标识符
的响应，但没有收到语音设备回复；随后发送的普通心跳则收到了对应回复。这只能说明
控制传输能工作、语音查询没有响应，不能证明该 MediaRemote 消息能导出 HomePod
实体麦克风。没有发送录音状态或语音上传命令；实现及协议参考见英文段落。

进一步测试仍未解决麦克风会话问题，没有可用的 HomePod 录音、Windows 输入设备或
可测量的麦克风延迟。67 项离线测试通过，只验证了模拟数据下的软件行为。
