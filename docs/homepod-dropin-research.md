# HomePod Check In / DropIn investigation

## Findings — 2026-11-14

The microphone is still not working in this fork. Public-source research has
identified a more specific investigation target: Apple's alarm-related **Check
In**, whose iOS client uses the private **DropIn** frameworks. This is a research
lead, not an implemented Windows recording backend.

The firmware follow-up below adds recovered message dictionaries and a
user-initiated session type; recording is still unverified.

### Documented functions

Apple documents that a HomePod can detect smoke/CO alarm sounds, notify a home
member's iPhone, iPad or Apple Watch, and let that member check in and talk to
people at home. The HomePod announces the check-in. The documented entry point
is an alarm notification, not a general microphone-recording command. [1]

Apple separately documents HomePod speakerphone and call handoff through an
iPhone or iPad. That establishes another supported audio-input use, but does not
document a Windows microphone API or the underlying network exchange. [2]

### Runtime declarations inspected

Inspected the original files in `MTACS/iOS-17-Runtime-Headers`, pinned to commit
`d1d960dfaa4107765dd7fcf891e4967c0930d5fd`. These are extracted **iOS 17**
declarations, not Apple's public SDK and not this HomePod's audioOS 27.2 binary.
They do not supply method bodies, wire payloads or cryptographic credentials.

| Source file | Evidence in the declaration |
| --- | --- |
| `HomeUI.framework/HUDropInViewController.h` [3] | The Home UI owns a DropIn center and active session, observes audio-analysis notifications, and handles session state and microphone controls. |
| `DropIn.framework/DIDropInSession.h` [4] | Explicit session start/end, session state, uplink mute status and a local XPC connection manager. |
| `DropIn.framework/DIDevice.h` [5] | Separate HomeKit home, HomeKit accessory and IDS identifiers. A LAN IP alone is not its device descriptor. |
| `DropInCore.framework/DropInCore.CallCenterManager.h` [6] | Active call/conversation, a conversation provider, a participant validator and TelephonyUtilities conversation callbacks. |
| `DropInCore.framework/DropInCore.IDSManager.h` [7] | An IDS service and incoming/outgoing message callbacks. |
| `HomeKit.framework/HMHomeAccessControl.h` [8] | A specific audio-analysis DropIn access level, distinct from general home access. |

**Inference:** a serious Check In implementation needs to understand authorized
home membership, device identities, session signalling and the media path. The
existing successful transient AirPlay authentication does not demonstrate any
of those DropIn permissions. Calling a named Objective-C method over local XPC
is not equivalent to sending that method name to HomePod's RTSP port.

The declarations justify investigating DropIn/IDS/call setup. They do **not**
establish the exact wire protocol, whether every session uses IDS or Rapport,
the media codec/encryption, the presence of these exact classes in 27.2, or
whether the alarm eligibility can support a user-initiated recording session.

### Existing projects and non-solutions

- `HomePod-Grok-Bot` explicitly captures speech on a separate Assist satellite;
  HomePod is output only. It does not solve the built-in microphone requirement. [9]
- The official checkra1n 0.12.1 release documents partial support for the
  **original** HomePod on audioOS 13/14. That historical result is not evidence of
  a working jailbreak or recorder for a second-generation HomePod on 27.2. [10]
- No reusable Windows implementation of the DropIn media/signalling path was
  identified in the projects and code searches examined for this follow-up.
  This is a scoped search result, not proof that none exists anywhere.

At that declaration-only stage, the missing artifacts were a version-matched DropIn implementation or an
authorized working-session trace showing message schemas, home/identity checks,
media negotiation, cryptography and session teardown. Until that is understood,
adding a GUI recording control or another guessed AirPlay format would not
establish a working HomePod microphone.

No DropIn session, alarm simulation, call, account login, permission change or
firmware change was performed in this public-source follow-up. This research
does not change the unsuccessful physical-device results in
[the existing microphone investigation](homepod-microphone-research.md).

## Firmware and implementation follow-up — 2026-11-14

The deeper pass went beyond the declarations above. It inspected public iOS
18.2 decompilation [11], actual FaceTime implementations [12][13], and an Apple
27.2 **24K122** restore image for **AudioAccessory5,1** [14]. The restore image is
for HomePod mini, **not AudioAccessory6,1**. Its shared-cache AirPlayReceiver
version is `1005.13.2.2.0`; the physical second-generation HomePod reports
AirPlay `1005.13.2`. These are useful matching-build artifacts, not proof that
the two models have identical behavior.

The image was downloaded and decrypted locally, then read with portable
7-Zip/APFS and `ipsw`; no firmware was installed or flashed. Extracted libraries,
disassembly and reports are research cache files under `.git`, not redistributed
Apple binaries. Hashes and locations are recorded in
[the evidence manifest](homepod-dropin-protocol-evidence.json).

### Concrete findings

- `DropInCore` version 169 has `StartDropInRequest` fields `homeIdentifier` and
  `sessionType`. The enum contains **soundAnalysis** and **userInitiated**.
  Thus the earlier alarm-notification-only investigation was too narrow. This
  does **not** establish that an arbitrary client can start a session: feature
  gates, authorization, current device state and media setup still apply.
- Its start-request dictionary uses `homeid` and `sessiontype`; the enum tags
  are 0 and 1 respectively. The response contains `sessionid`, `homeid`,
  `devicePseudonym`, `devicePseudonymExpiration`, and optional archived `error`.
- The outer message dictionary uses `mid`, `pid` and `p`. The Rapport service
  is `com.apple.dropin.setup`, its request identifier prefix is
  `com.apple.dropin.setup.request.` **including the trailing dot**, and its
  IDS service is `com.apple.private.alloy.dropin.setup`. These are protocol
  identifiers, not HTTP/RTSP endpoints.
- The matching-build mini AirPlayReceiver's session-setup dispatcher handles
  stream types 96, 103, 110, 120 and 130. Type 100 takes the unsupported-stream
  branch, which logs and continues rather than necessarily failing the entire
  request. This is a concrete explanation consistent with the physical
  HomePod's type-100 `200`/empty-response observations; the model distinction
  prevents treating it as a disassembly of that physical speaker.
- The 27.2 DropIn library still contains home-access, DropIn-access,
  remote-access, sender identity and participant validation paths. The public
  iOS 18.2 method bodies explicitly check home access and DropIn permission,
  with additional remote-access checks for the remote transport. [11]

Binary locations retained for reproduction:

| Artifact | Unslid address | Finding |
| --- | --- | --- |
| DropInCore | `0x19e56f1c8` / `0x19e56f1f0` | Start request / session-type reflection descriptors. |
| DropInCore | `0x19e4d5400` | Start-request dictionary construction. |
| DropInCore | `0x19e535da8` | Message-container dictionary construction. |
| DropInCore | `0x19e5366a4` | Rapport service-type getter. |
| AirPlayReceiver | `0x1b477a720` / `0x1b477aaec` | Audio stream-type dispatch / unsupported-stream branch. |

### Code completed and limits

`tools/homepod_dropin_messages.py` implements the recovered start-request,
message-envelope and response dictionary handling. Its 28 constructed-fixture
tests pass; the existing 67 microphone-probe/capture tests still pass. The
module does **not** open a socket,
authenticate a home member, serialize an OPACK frame, start a call, or capture
audio. The tests are offline schema tests, not successful device recordings.

Rustpush has real IDS/FaceTime setup, SRTP and incoming media-frame handling;
its source has no HomePod/DropIn integration in the inspected checkout [12].
OpenClaw FaceTime has real audio capture, but it targets an active call process
on Apple Silicon macOS, not a Windows-to-HomePod microphone connection [13].
Neither is a ready recording backend for this fork. They make the call-media
route more concrete, while authorized home identity and DropIn-to-call/media
integration remain unimplemented.

The physical HomePod was reachable again in this pass (`GET /info` and
`OPTIONS` returned 200 at 08:24 UTC). No new DropIn or call session was sent.
No HomePod microphone audio has been received, so recording and microphone
latency remain unverified.

## Companion transport and physical tests — 2026-11-14

`tools/homepod_companion_probe.py` now implements a bounded Companion client:
unicast discovery on the selected interface, OPACK framing, X25519/Ed25519
pair-verify with an explicitly supplied pinned identity, public transient SRP,
server-proof verification, ChaCha20-Poly1305 framing with header AAD, encrypted
system information, and a routing-only DropIn state probe with session cleanup.
These are control-channel functions, **not a working recording backend**.

Physical second-generation HomePod results, 09:09–09:20 UTC:

- Unicast discovery found Companion port 49153, `rpFl=0x62792` and `rpVr=715.2`.
  The earlier multicast miss was not evidence that Companion was absent.
- Pair-verify M2 could be decrypted; a fresh, unpaired controller identity was
  rejected in M4 with TLV state 4 / error 2 (authentication).
- A different path recovered from `RPConnection`'s public pairing branch [15]
  **worked**: `_pwTy=0`, transient flag `0x10`, SRP fixed public value `public`,
  and `ClientEncrypt-main` / `ServerEncrypt-main` stream keys. The received
  server proof was verified. No M5/M6 persistent credential exchange was sent.
  Thus it would be incorrect to claim that all Companion access requires an
  existing paired identity. This public guest session is not home membership.
- Encrypted `_systemInfo` returned the correct model but no HomeKit identity,
  HomeKit user identity, or service list. A logical Rapport session for
  `com.apple.dropin.setup` could be opened. A `DropInStateRequest` deliberately
  **omitting required `homeid`** returned `RPErrorDomain`, code **58822**.
  The library's protocol documentation associates this with no request handler
  [16]. This is not a valid DropIn start test or proof of a particular permission
  rejection: service routing, feature state and authorization remain distinct.
- The final logical session closed successfully; the TCP connection then closed.
  Some earlier full-range local session IDs produced `Session not found` on
  cleanup. Restricting the local half to 31 bits yielded successful cleanup;
  the precise server-side cause is not established. Post-test AirPlay `/info`
  and `OPTIONS` both returned 200. No DropIn call or audio session was started.

The name convention was also resolved statically: a conformance witness at
`0x19e550450` calls `String(describing: Self.self)` through `0x19e5503f8` for
`StartDropInRequest`; `0x19e524c44` appends the name to the request prefix. The
start request identifier is consequently
`com.apple.dropin.setup.request.StartDropInRequest`. This static conclusion
has not been validated by an accepted, authorized start on the physical device.

Reproduce the public diagnostic from the repository root, using actual target
and local Wi-Fi IPv4 addresses and a **new report filename**:

```powershell
.git/mic-venv/Scripts/python.exe -B tools/homepod_companion_probe.py --host <HOMEPOD_IP> --local-ip <WINDOWS_WIFI_IP> --public-transient --probe-dropin-route --report .git/companion-result.json
.git/mic-venv/Scripts/python.exe -B -m unittest discover -s tools/tests -p 'test_homepod_*.py' -q
```

Exit 3 means the routing-only diagnostic finished without establishing usable
DropIn access; exit 2 means a handshake or cleanup error. Omitting
`--probe-dropin-route` performs only public encrypted system-info diagnostics.
`--test-unpaired-identity` is a separate single-attempt identity diagnostic;
`--credentials-file` accepts an existing identity explicitly provided by its
owner and does not obtain or provision that identity. The tool does not search
profiles or keychains. Credential JSON has exactly four hex fields:
`server_identifier`, `server_public_key`, `client_identifier`,
`client_private_key` (32-byte Ed25519 seed). Keep such files private.

All **117** offline tests pass, including **22** new Companion tests. They cover
cryptographic signatures and proofs, wrong pinned identities, nonce reuse,
fragmented and oversized frames, actual OPACK integer subclasses, encryption,
and cleanup after a state-request timeout. The independently recorded physical
outcomes are in [the sanitized device result](homepod-companion-device-result.json).
No valid home UUID, authorized DropIn call, received microphone media, decoded
recording or measured microphone latency is available. Home-member identity
provisioning and the DropIn-to-call/media integration remain unimplemented.

## 中文：Check In / DropIn 调研

### 结论 — 2026-11-14

本 fork 仍未实现 HomePod 自带麦克风录音。这次联网调研找到了更具体的研究对象：
苹果的告警后“查看情况（Check In）”，以及 iOS 客户端使用的私有 DropIn 框架。
这是协议线索，尚不是可用的 Windows 录音后端。

苹果官方说明，HomePod 检测到烟雾/一氧化碳警报声后，可通知家庭成员的 iPhone、
iPad 或 Apple Watch，成员可进入查看情况并与家中人员通话；HomePod 会播报有人
正在查看。文档入口是告警通知，没有描述任意开启录音的通用命令。[1]

另一条官方路径是通过 iPhone/iPad 使用 HomePod 免提及接力通话。这说明 HomePod
确有音频输入用途，但没有提供 Windows 麦克风 API 或网络握手细节。[2]

### 查到的具体代码依据

实际下载并检查了上方固定提交中的 iOS 17 运行时头文件。它们是提取出的声明，
不是苹果公开 SDK，也不是这台音箱 audioOS 27.2 的程序；不包含方法实现、网络报文
或密钥。文件及来源对应上方表格：

- Home UI 的 `HUDropInViewController` 持有 DropIn 会话并处理声音分析通知。[3]
- `DIDropInSession` 有启动、结束、会话状态、上行静音及本地 XPC 管理对象。[4]
- `DIDevice` 同时保存家庭、HomeKit 设备和 IDS 标识，不是只有 IP 的描述结构。[5]
- 核心模块出现通话会话、参与者校验器、TelephonyUtilities 回调以及 IDS 消息处理。[6][7]
- 家庭权限对象单独声明了声音分析 DropIn 访问等级。[8]

**由此推断**，继续实现需要弄清家庭成员授权、设备身份、会话信令及媒体传输。
目前临时 AirPlay 鉴权成功，不能证明已经获得 DropIn 权限；本地 XPC 方法名也不能
直接当作音箱 RTSP 端口上的命令发送。

这些声明没有证明确切的线上报文、每次会话是否使用 IDS/Rapport、音频编码/加密、
27.2 是否保留相同实现，或者告警限定的会话能否用于主动开始录音。这些仍需验证。

### 排除的项目及剩余工作

`HomePod-Grok-Bot` 明确使用独立 Assist 麦克风，HomePod 只负责播放，不能解决本机
麦克风需求。[9] checkra1n 官方旧版本只说明原版 HomePod、audioOS 13/14 的部分支持，
不能据此声称二代 HomePod 27.2 已能越狱录音。[10] 本轮检查的项目和代码搜索中，
没有找到可直接复用的 Windows DropIn 信令/媒体实现；这不是对全网不存在的证明。

该轮声明检查之后所缺的是匹配版本的 DropIn 实现，或经过授权、已成功会话的协议记录，以确认
消息格式、身份校验、媒体协商、加密及清理。没有这些依据时，新增录音按钮或继续
猜测 AirPlay 格式仍不能构成可用录音功能。

这轮仅查询公开资料，没有发起 DropIn、模拟警报、拨打电话、登录账户、修改权限或
固件；不改变现有真机测试“没有收到音频”的结论。

### 固件与实现深入复查 — 2026-11-14

这轮进一步检查了公开的 iOS 18.2 反编译方法体 [11]、实际 FaceTime 实现 [12][13]，
并下载、解密和静态分析了苹果的 27.2 / **24K122** 恢复固件 [14]。固件型号是
**HomePod mini（AudioAccessory5,1）**，不是用户的二代 **AudioAccessory6,1**。
固件中的 AirPlayReceiver 版本为 `1005.13.2.2.0`，真机报告为 `1005.13.2`；这是
相同构建的协议依据，不能声称已经反编译了用户这台音箱或两种型号行为完全一致。
全程为本地文件分析，没有刷机。提取的苹果二进制和汇编只保留在 `.git` 研究缓存，
不提交分发；哈希和地址见 [证据清单](homepod-dropin-protocol-evidence.json)。

新的具体发现：

- DropInCore 169 的请求中确实包含 `soundAnalysis` 和 **`userInitiated`（用户主动发起）**
  两种会话类型。因此先前只追踪告警通知入口不够完整；但枚举存在不等于任意客户端
  都能发起，还需验证功能开关、权限、设备状态与媒体建立。
- 请求字典字段为 `homeid`、`sessiontype`，两种类型的标签分别为 0、1。响应有
  `sessionid`、`homeid`、`devicePseudonym`、`devicePseudonymExpiration` 及可选的
  归档错误数据 `error`；外层消息使用 `mid`、`pid`、`p`。
- Rapport 服务为 `com.apple.dropin.setup`，请求标识前缀为
  `com.apple.dropin.setup.request.`（**保留结尾的点**），IDS 服务为
  `com.apple.private.alloy.dropin.setup`。这些是协议标识，不是 HTTP/RTSP 接口。
- 同构建 mini 的 AirPlay 接收端分派 96、103、110、120、130 类型；100 会进入
  unsupported-stream 分支，记录后继续，不一定使整个请求失败。这与二代真机此前
  type 100 返回 200、空响应且无音频的现象一致，但仍需保留不同型号的证据边界。
- 27.2 的 DropIn 库仍包含家庭、DropIn、远程访问、发送者身份与参与者校验路径；
  iOS 18.2 方法体可直接看到家庭和 DropIn 权限检查，远程传输还检查远程访问权限。[11]

代码新增 `tools/homepod_dropin_messages.py`，实现已确认的请求、外层消息和响应字典
处理，28 项构造样本测试通过；原有 67 项麦克风探测/采集测试仍通过。该字典模块没有
网络连接、OPACK 帧编码、家庭身份认证、拨号或
录音能力；测试通过不能当作真机录音成功。

Rustpush 已有真实 IDS/FaceTime 建连、SRTP 和接收媒体帧处理，但检查的代码没有
HomePod/DropIn 对接 [12]。OpenClaw FaceTime 能采集通话音频，但运行于 Apple Silicon
macOS 的活跃通话进程，不是 Windows 直连 HomePod 麦克风 [13]。两者是可继续接入的
通话媒体实现，不是现成解法；家庭身份授权及 DropIn 到通话/媒体的对接仍未实现。

这轮北京时间 16:24 的只读复查中，二代真机的 `/info`、`OPTIONS` 均返回 200，设备
仍可访问；没有发送新的 DropIn/通话会话。目前依旧未收到 HomePod 麦克风音频，
不能报告录音完成，也不能提供实测麦克风延迟。

### Companion 通道实现与真机测试 — 2026-11-14

新增 `tools/homepod_companion_probe.py`，实现指定网卡的单播发现、OPACK 帧、固定身份
密钥验证、X25519/Ed25519、公共临时 SRP、服务端证明校验、带帧头认证的
ChaCha20-Poly1305 加密、系统信息查询，以及带清理的 DropIn 只读路由诊断。
这些是控制通道实现，**仍不是可用录音后端**。

北京时间 17:09–17:20 对这台二代 HomePod 的实际结果：

- 定向查询找到 Companion 端口 49153，`rpFl=0x62792`、`rpVr=715.2`；此前组播扫描
  漏掉服务，不能据此判定它不存在。
- 可解密 Pair-Verify M2；新生成、未配对的控制端身份在 M4 收到 state 4、error 2，
  即身份认证失败。
- 继续检查 `RPConnection` 的公共配对分支 [15] 后，**另一条路径已实际连通**：
  `_pwTy=0`、临时标志 `0x10`、SRP 固定公共值 `public`，再派生 Companion 加密
  密钥。已校验收到的服务端证明，没有发送持久配对的 M5/M6。因此不能说所有
  Companion 访问都必须已有配对身份；但公共访客会话不等于家庭成员身份。
- 加密 `_systemInfo` 返回正确型号，没有家庭设备身份、家庭用户身份或服务列表。
  可以建立 `com.apple.dropin.setup` 逻辑会话，但故意**省略必填 `homeid`** 的
  `DropInStateRequest` 返回 `RPErrorDomain / 58822`。协议库文档将其解释为没有
  请求处理器 [16]。这不是有效的 DropIn 启动测试，也不能直接当作某项权限被拒的
  证明；路由、功能状态、身份授权仍需区分。
- 最后一次逻辑会话正常关闭，随后关闭 TCP；较早使用完整 32 位随机本地会话 ID
  时，部分清理请求得到 `Session not found`。限制本地部分为 31 位后清理成功，
  尚未确定服务端具体原因。测试后 AirPlay `/info`、`OPTIONS` 都返回 200。
  没有启动 DropIn 通话或音频会话。

静态分析也确认了名称规则：`0x19e550450` 经 `0x19e5503f8` 对请求类型调用
`String(describing: Self.self)`，`0x19e524c44` 将名称追加到请求前缀；因此启动标识
为 `com.apple.dropin.setup.request.StartDropInRequest`。这是静态结论，尚未获得
二代真机接受的、经过授权的启动响应。

复现命令见英文节；须填实际音箱 IP、Windows Wi-Fi IP，并使用新的报告文件名。
退出码 3 表示路由诊断完成但未建立可用 DropIn 权限，2 表示握手或清理错误。
去掉 `--probe-dropin-route` 只查询公共加密系统信息；`--test-unpaired-identity`
单独执行一次未配对身份测试；`--credentials-file` 仅接受所有者明确提供的既有身份，
不负责获取或注册身份，也不搜索用户配置或钥匙串。凭据文件的四个十六进制字段为
`server_identifier`、`server_public_key`、`client_identifier`、`client_private_key`
（32 字节 Ed25519 种子），应私密保存。

共 **117** 项离线测试通过，其中 Companion 新增 **22** 项，涵盖签名与证明、错误
固定身份、随机数重复使用防护、分段与超大帧、真实 OPACK 整数子类、加密，以及
状态请求超时后的清理。真机结果另存于 [脱敏报告](homepod-companion-device-result.json)。
目前仍没有有效家庭 UUID、获授权的 DropIn 通话、麦克风媒体包、解码录音或实测
麦克风延迟；Windows 家庭身份注册和 DropIn 到通话媒体的对接仍未实现。

## Sources / 来源

1. Apple, Recognize sounds using HomePod: <https://support.apple.com/guide/homepod/recognize-sounds-apd1f1921722/homepod>
2. Apple, Use HomePod for phone calls: <https://support.apple.com/en-afri/guide/homepod/apdeaa15a6c3/homepod>
3. <https://github.com/MTACS/iOS-17-Runtime-Headers/blob/d1d960dfaa4107765dd7fcf891e4967c0930d5fd/PrivateFrameworks/HomeUI.framework/HUDropInViewController.h>
4. <https://github.com/MTACS/iOS-17-Runtime-Headers/blob/d1d960dfaa4107765dd7fcf891e4967c0930d5fd/PrivateFrameworks/DropIn.framework/DIDropInSession.h>
5. <https://github.com/MTACS/iOS-17-Runtime-Headers/blob/d1d960dfaa4107765dd7fcf891e4967c0930d5fd/PrivateFrameworks/DropIn.framework/DIDevice.h>
6. <https://github.com/MTACS/iOS-17-Runtime-Headers/blob/d1d960dfaa4107765dd7fcf891e4967c0930d5fd/PrivateFrameworks/DropInCore.framework/DropInCore.CallCenterManager.h>
7. <https://github.com/MTACS/iOS-17-Runtime-Headers/blob/d1d960dfaa4107765dd7fcf891e4967c0930d5fd/PrivateFrameworks/DropInCore.framework/DropInCore.IDSManager.h>
8. <https://github.com/MTACS/iOS-17-Runtime-Headers/blob/d1d960dfaa4107765dd7fcf891e4967c0930d5fd/Frameworks/HomeKit.framework/HMHomeAccessControl.h>
9. <https://github.com/masonearl/HomePod-Grok-Bot/blob/main/docs/ARCHITECTURE.md>
10. checkra1n 0.12.1 beta: <https://checkra.in/releases/0.12.1-beta>
11. iOS 18.2 DropInCore decompilation, pinned public research source (not an Apple SDK): <https://github.com/EthanArbuckle/iPhone17-1_18.2_22C152_Restore/blob/e26ed4563f78871c59d2d96856756a65d62517e5/System/Library/PrivateFrameworks/DropInCore.framework/DropInCore.m>
12. Rustpush, inspected commit `b3c96c23e1aae78d1229ebaccb12027624f1eaff`: <https://github.com/OpenBubbles/rustpush/tree/b3c96c23e1aae78d1229ebaccb12027624f1eaff>; media implementation: `src/avconference.rs`, signalling: `src/facetime.rs`.
13. OpenClaw FaceTime, inspected commit `0864ed3d4547203eb16f31c0d18def8c4338cc42`: <https://github.com/openclaw/openclaw-facetime/tree/0864ed3d4547203eb16f31c0d18def8c4338cc42>.
14. Apple restore image, AudioAccessory5,1 27.2 / 24K122: <https://updates.cdn-apple.com/2026FallFCS/4ee5d3c9-158b-4ad9-95b4-6cfa9ac0f0d4/AudioAccessory5,1_27.2_24K122_Restore.ipsw>.
15. Public iOS 18.2 `RPConnection` method bodies, same pinned research repository: <https://github.com/EthanArbuckle/iPhone17-1_18.2_22C152_Restore/blob/e26ed4563f78871c59d2d96856756a65d62517e5/System/Library/PrivateFrameworks/Rapport.framework/RPConnection.m>.
16. pyatv protocol documentation, Companion framing and no-handler errors: <https://pyatv.dev/documentation/protocols/>.
