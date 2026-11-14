# HomePod Check In / DropIn investigation

## Findings — 2026-11-14

The microphone is still not working in this fork. Public-source research has
identified a more specific investigation target: Apple's alarm-related **Check
In**, whose iOS client uses the private **DropIn** frameworks. This is a research
lead, not an implemented Windows recording backend.

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

The next missing artifacts are a version-matched DropIn implementation or an
authorized working-session trace showing message schemas, home/identity checks,
media negotiation, cryptography and session teardown. Until that is understood,
adding a GUI recording control or another guessed AirPlay format would not
establish a working HomePod microphone.

No DropIn session, alarm simulation, call, account login, permission change or
firmware change was performed in this public-source follow-up. This research
does not change the unsuccessful physical-device results in
[the existing microphone investigation](homepod-microphone-research.md).

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

下一步所缺的是匹配版本的 DropIn 实现，或经过授权、已成功会话的协议记录，以确认
消息格式、身份校验、媒体协商、加密及清理。没有这些依据时，新增录音按钮或继续
猜测 AirPlay 格式仍不能构成可用录音功能。

这轮仅查询公开资料，没有发起 DropIn、模拟警报、拨打电话、登录账户、修改权限或
固件；不改变现有真机测试“没有收到音频”的结论。

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
