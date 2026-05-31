# RM2026 Wireless Link

RM2026 Wireless Link 是 RoboMaster 2026 雷达站无线链路解析模块，用于接收官方空口干扰波和信息波，解析干扰密钥、机器人坐标、血量、经济、剩余发弹量等信息，并支持通过裁判系统链路上传干扰密钥。

## 功能概述

- 支持 `jam1 -> jam2 -> info` 顺序接收策略。
- 支持蓝方/红方无线频点和干扰等级参数。
- 支持 ANTSDR E200 / Pluto 兼容 IIO 设备实时接收，也支持 `.c64` 录波离线解析。
- 支持一级、二级干扰波密钥解析与上传。
- 支持三级后切换信息波解析，输出坐标、血量、经济、占领状态、发弹量、Buff 与哨兵姿态。
- 支持记录 IQ 原始录波，便于赛后复盘。

## 硬件与设备

实机运行需要：

- ANTSDR E200，用于接收无线波形。
- 430-440 MHz 频段八木天线，用于增强比赛无线链路接收质量。
- 裁判系统串口，用于读取干扰等级并上传密钥。
- Ubuntu/Linux 主机，推荐使用系统 Python 运行 GNU Radio 相关脚本。

离线解析录波只需要本仓库代码和 GNU Radio Python 环境，不需要连接 SDR 或裁判系统。

## 环境依赖

推荐环境：

```text
OS: Ubuntu 22.04
Python: /usr/bin/python3
GNU Radio: 3.10+
pyadi-iio / libiio: ANTSDR E200 / Pluto 兼容 IIO 设备实时接收时需要
pyserial: 裁判系统串口通信时需要
numpy: 离线录波解析时需要
```

运行命令建议使用：

```bash
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 ...
```

这样可以避免 Conda 环境污染 GNU Radio 的 Python 动态库路径。

## 文件目录结构

```text
.
├── README.md                         # 项目说明
├── config.yaml                       # 单独运行无线模块的默认配置
├── tools/
│   ├── sequential_single_freq_rx.py  # 比赛实时接收入口
│   ├── field_recording_parse.py      # 单个 IQ 录波离线解析工具
│   ├── field_recording_regression.py # 录波回归测试
│   ├── protocol_regression.py        # 协议打包/解析回归测试
│   ├── replay_recorded_wireless_status.py # 将录波解析结果回放成 wireless.json
│   ├── replay_iq_tx.py               # IQ 回放/发射调试工具
│   └── wireless_status_dashboard.py  # 无线状态终端看板
└── wireless_link/
    ├── __init__.py
    ├── protocol.py                   # RM2026 空口帧与裁判帧协议
    ├── decoder.py                    # bit 流扫描、partial frame 恢复、信息波字段提取
    ├── field_recording.py            # `.c64` 录波离线 GFSK 解调
    ├── pluto_rx.py                   # IIO SDR 实时接收 flowgraph
    ├── pluto_ensm.py                 # IIO SDR ENSM 状态控制
    ├── referee_link.py               # 裁判系统串口通信与发包
    └── sequential_receiver.py        # 比赛状态机：按裁判等级切换 jam/info
```

## 比赛实时运行

在仓库根目录运行：

```bash
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 \
  tools/sequential_single_freq_rx.py \
  --team blue \
  --rx-uri ip:192.168.2.10 \
  --referee-port /dev/ttyACM0 \
  --status-out runtime/wireless_status.json
```

常用参数：

- `--team blue/red`：选择己方颜色，对应不同官方频点。
- `--rx-uri ip:192.168.2.10`：SDR 设备地址。
- `--referee-port /dev/ttyACM0`：裁判系统串口。
- `--status-out runtime/wireless_status.json`：持续写出的无线状态文件。
- `--window-s 1.5`：一级/二级干扰波接收窗口。
- `--info-window-s 0.75`：三级后信息波接收窗口。
- `--record-iq-dir recordings/manual/wireless`：保存实时 IQ 原始录波。

无裁判系统串口时，可以手动指定阶段调试：

```bash
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 \
  tools/sequential_single_freq_rx.py \
  --team blue \
  --no-referee \
  --manual-stage jam2 \
  --loops 1
```

## 离线解析录波

解析单个干扰波录波：

```bash
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 \
  tools/field_recording_parse.py \
  --iq-in recordings/data/20260530_175801/wireless/jam1_20260530_180019_loop000000.c64 \
  --mode jam \
  --team blue \
  --jam-level 1 \
  --sample-rate 2083333
```

解析单个信息波录波：

```bash
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 \
  tools/field_recording_parse.py \
  --iq-in recordings/data/20260530_182117/wireless/info_20260530_182247_loop000023.c64 \
  --mode info \
  --team blue \
  --jam-level 3 \
  --sample-rate 2083333 \
  --rx-preprocess dc_block
```

输出 JSON：

```bash
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 \
  tools/field_recording_parse.py \
  --iq-in recordings/data/20260530_182117/wireless/info_20260530_182247_loop000023.c64 \
  --mode info \
  --team blue \
  --jam-level 3 \
  --sample-rate 2083333 \
  --rx-preprocess dc_block \
  --json-out runtime/info_decode.json
```

## 解析结果说明

信息波会解析以下 `cmd_id`：

- `0x0A01`：机器人坐标，单位 cm。
- `0x0A02`：机器人血量。
- `0x0A03`：剩余发弹量。
- `0x0A04`：经济与占领状态。
- `0x0A05`：Buff、冷却和哨兵姿态。
- `0x0A06`：干扰波密钥。

实时接收时，状态文件会持续写出：

- `strict_key` / `recovered_key`：干扰波密钥解析结果。
- `info_positions_cm`：信息波坐标。
- `info_economics`：经济与占领状态。
- `info_remaining_bullets`：剩余发弹量。
- `frame_counts` / `partial_frame_counts`：完整帧和 partial 帧统计。

## 配置文件

`config.yaml` 是单独运行无线模块时的默认配置。比赛集成启动时通常由上层 `radar_competition` 生成参数并覆盖命令行。

路径相关字段建议使用相对路径，例如：

```yaml
status_out: runtime/rm2026_wireless_status.json
recording:
  enabled: false
  dir: recordings/manual_wireless
```

## 设计要点

- 默认空口 bit 顺序为 `msb_all`。
- TX CRC8 使用 `reflected`。
- RX CRC8 自动识别，兼容历史录波。
- `recovered_key` 默认只作为诊断信息，只有显式开启 `--allow-recovered-key-upload` 才会上传。
- 信息波离线解析建议开启 `--rx-preprocess dc_block`，可降低 DC 偏置对 GFSK 解调的影响。

## 致谢

感谢 PnX-HKUST(GZ) RoboMaster 战队在 RM2026 赛季无线链路测试、录波采集和赛场验证中提供的硬件、场地与调试支持。
