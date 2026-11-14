# HomePod microphone uplink research / HomePod 麦克风上行研究

## Status — 2026-11-14

**This fork does not record the HomePod microphone.** This is a feasibility
investigation for a second-generation HomePod, not a working recording feature.
Read-only metadata queries succeeded against one physical HomePod; no microphone
audio has been received and no authenticated microphone session was attempted.

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

## What a working implementation still needs

1. Confirm the HomePod's IP, advertised AirPlay port and audioOS build, and
   inspect the real metadata report. Do not substitute fixture data for hardware.
2. Identify an authenticated microphone session and its transport. If this
   requires reproducing Apple's call handoff, investigate that separately from
   the existing playback sender; do not invent an endpoint or capability bit.
3. Demonstrate actual HomePod-originated audio: controlled speech, tap tests and
   Windows-input isolation must distinguish it from a PC mic or playback echo.
4. Only then implement decoding, jitter buffering, WAV recording and clean stop.
   Using it in other Windows apps additionally needs a virtual input endpoint;
   writing a WAV file alone does not create a Windows microphone device.

No completion date or recording support is claimed until step 3 passes.

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
The Windows capture implementation has not been changed or tested as part of
this research; only the standalone Python probe and its tests were added.

## 当前状态 — 2026-11-14

**这个 fork 尚不能录制 HomePod 自带麦克风。** 当前是二代 HomePod 的可行性研究，
已成功读取一台真实 HomePod 的设备信息，但没有录到任何 HomePod 麦克风音频，
也尚未尝试经过鉴权的麦克风会话。
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

### 距离真正录音还缺什么

1. 确认 IP、AirPlay 端口及 audioOS 版本，取得真实设备信息，不能用模拟数据代替。
2. 找到并验证经过鉴权的麦克风会话和音频传输通道；若依赖苹果通话接力协议，需要
   单独研究，不能凭空假设一个接口或能力位。
3. 通过说话、敲击及隔离电脑输入设备，确认收到的确实是 HomePod 麦克风，而不是
   电脑麦克风或播放回声。
4. 再实现解码、抖动缓冲、WAV 保存和正常停止。若要让其他 Windows 软件选择它作为
   麦克风，还需要虚拟录音端点；保存 WAV 并不会自动创建 Windows 输入设备。

在第 3 步通过前，不承诺录音成功或交付日期。

### 真机观察 — 2026-11-14

电脑连接音箱所在 Wi-Fi 后，通过 mDNS 找到型号 `AudioAccessory6,1`，广播软件版本
`27.2`、AirPlay 版本 `1005.13.2`。IPv4 和 IPv6 的 `GET /info`、`OPTIONS *`
都返回 200，设备信息确认构建号 `24K122`。没有执行配对、建立播放会话或调整音量。

本次设备信息没有 `audioFormats`、`audioLatencies` 或明确的麦克风输入字段，
有 `supportedFormats`、`supportedAudioFormatsExtended` 及播放能力字段。这不足以
排除所有私有麦克风通道。`OPTIONS` 列出了 `RECORD`，仍不能作为支持录音的证据。

设备名称、地址、标识符、原始 plist 和网络发现记录不提交到仓库；本地诊断报告保存在
`.git/` 内。这次没有修改或测试 Windows 采集实现，只增加独立 Python 探测工具及测试。
