# Cisco Small Business 交换机 Netmiko 分页修复设计

日期：2026-08-14  
状态：已批准并实施

## 1. 背景与根因

运维助手查询 `10.11.210.67` 的运行配置时，Netmiko 抛出 `ReadTimeout`，等待的提示符正则中包含 `\x1b[K`：

```text
Pattern not detected: '\x1b\\[KECNCD210\\-67\\#'
```

该设备的实际型号是 Cisco SG350X-24-K9。它属于 Cisco Small Business 产品线，使用的 CLI 平台不是 IOS-XE。当前 CMDB 把设备标为 `cisco_iosxe`，后端因此选择 Netmiko `cisco_xe` 驱动。这个驱动使用 IOS/IOS-XE 的会话初始化方式：发送 `terminal length 0`，并且不会主动开启 ANSI 清洗。

Netmiko 4.7 已提供专用的 `cisco_s300` 驱动，覆盖 Cisco S200/S300/S500 及相同 CLI 家族。该驱动的官方初始化顺序是：

1. 开启 `ansi_escape_codes`，清除 `ESC[K` 等终端控制符。
2. 识别并缓存设备提示符。
3. 发送 `terminal width 511`。
4. 发送 `terminal datadump` 关闭分页。

Cisco SG350X 的 CLI 参考手册同样将 `terminal datadump` 定义为关闭分页的命令。因此本次故障的根因是设备平台建模错误，而不是静态密码、网络读速率或 `read_timeout` 太短。

参考资料：

- [Netmiko Cisco S300 驱动 API](https://ktbyers.github.io/netmiko/docs/netmiko/cisco/cisco_s300.html)
- [Netmiko 支持的平台列表](https://github.com/ktbyers/netmiko/blob/develop/PLATFORMS.md)
- [Cisco Sx350X CLI 参考手册](https://www.cisco.com/c/dam/en/us/td/docs/switches/lan/csbms/350xseries/2_5_10/CLI_350x_ver2_5_10.pdf)

## 2. 目标与非目标

### 2.1 目标

- 让 SG350X 等 Cisco Small Business 交换机使用 Netmiko 官方 `cisco_s300` 驱动。
- 由官方驱动完成 ANSI 清洗、提示符识别和 `terminal datadump` 分页关闭。
- 保持真正的 IOS-XE 设备继续使用 `cisco_xe`，避免用端口数量猜测设备平台。
- 让 CMDB 前后端可以明确选择 Cisco Small Business 平台。
- 为驱动映射、命令目录、API 校验和前端表单补充回归测试。

### 2.2 非目标

- 不实现业务层通用 `--More--` 自动翻页循环。
- 不启用 Netmiko `session_log`；运行配置可能包含敏感信息，不应默认写入额外日志文件。
- 不自动探测设备类型；自动探测会增加额外连接和探测命令，也不能替代 CMDB 的确定性平台信息。
- 不批量修改所有 `cisco_iosxe` 资产。24 口只表示端口规模，不能证明设备运行 Small Business CLI。
- 自动化测试不连接真实交换机。

## 3. 平台建模

在现有 `VendorName` 中新增：

```text
cisco_small_business
```

该值表示 Cisco Small Business CLI 家族，而不是单一型号。后端映射为：

```text
cisco_small_business -> cisco_s300
cisco_iosxe          -> cisco_xe
```

选择单独的平台值而不是把 `cisco_iosxe` 直接改成 `cisco_s300`，原因是两类设备的分页命令、ANSI 行为和部分 CLI 交互不同。CMDB 的 `vendor` 数据库列已经是 `VARCHAR(50)`，新增枚举值不需要数据库结构迁移。

前端厂商下拉新增：

```text
思科 Small Business（SG350X 等）
```

后端枚举是权威来源，前端 TypeScript 类型、Zod 枚举、表单选项和旧值解析列表必须同步更新。

## 4. 命令目录

为 `cisco_small_business` 登记经过 Cisco CLI 文档确认的命令：

| 语义命令 | SG350X CLI | 说明 |
| --- | --- | --- |
| `show_version` | `show version` | 只读 |
| `show_running_config` | `show running-config` | 只读，驱动会先执行 `terminal datadump` 关闭分页 |
| `show_interfaces` | `show interfaces status` | 只读 |
| `ping` | `ping ip 1.1.1.1` | 固定目标，避免用户输入变成探测跳板 |
| `reboot` | `reload` | 复用 HITL，确认提示按 `(Y/N)` 匹配并发送 `y` |
| `port_enable` | `interface {interface}` + `no shutdown` | 复用接口名白名单校验 |
| `port_disable` | `interface {interface}` + `shutdown` | 复用接口名白名单校验 |

整机 `shutdown` 仍不支持网络交换机，不为新平台登记。

## 5. 执行数据流

```text
Agent 提出 device_query
  -> CMDB 资产返回 vendor=cisco_small_business
  -> 命令目录验证该平台支持语义命令
  -> executors 映射 device_type=cisco_s300
  -> Netmiko CiscoS300SSH.session_preparation
       -> ANSI 清洗
       -> 提示符识别
       -> terminal width 511
       -> terminal datadump
  -> send_command("show running-config")
  -> 读取到干净提示符并返回完整配置
  -> 现有输出截断与敏感错误隔离逻辑保持不变
```

不增加自定义提示符正则或分页循环。这样驱动差异仍由 Netmiko 的平台类负责，业务执行器只负责确定性地选择正确驱动。

## 6. 失败处理与可观测性

- 驱动建连、认证或 `session_preparation` 失败时，沿用现有 `ExecutionResult` 分类和服务端异常堆栈。
- 不把密码、配置正文或 Netmiko 原始异常文本返回给 Agent。
- 不通过增加 `read_timeout` 掩盖驱动错误。
- 如果资产仍错误标为 `cisco_iosxe`，系统不会按 IP 或端口数偷偷改写；运维人员必须在 CMDB 中修正平台。
- 文档明确说明 `vendor` 实际表达“厂商 CLI 平台”，选择错误会导致提示符、分页和变更命令不匹配。

## 7. 测试设计

所有生产代码都在对应失败测试之后实现。

### 7.1 后端

- `VendorName`/Pydantic schema 接受 `cisco_small_business`。
- `_netmiko_device_type_for_vendor("cisco_small_business")` 返回 `cisco_s300`，而 `cisco_iosxe` 仍返回 `cisco_xe`。
- `_open_netmiko_connection` 将 `device_type="cisco_s300"` 传给 `ConnectHandler`。
- 命令目录为新平台返回正确的只读、重启和端口命令。
- 不支持的整机 `shutdown` 在连接前失败关闭。
- 现有 IOS-XE、华为、H3C、Junos 测试保持通过。

### 7.2 前端

- `VendorName` 和 Zod 枚举接受 `cisco_small_business`。
- CMDB 编辑表单能够回显并提交该平台。
- 厂商下拉显示“思科 Small Business（SG350X 等）”。

### 7.3 验证命令

- 后端先运行与命令目录、CMDB schema、执行器有关的定向测试，再运行完整测试集。
- 前端运行与 CMDB 表单有关的定向测试，再运行 lint、类型检查和完整测试集。
- 不在自动测试中使用 `10.11.210.67` 的真实密码或建立真实 SSH 连接。

## 8. 上线与实机验收

代码部署后，在 CMDB 编辑 `10.11.210.67`：

```text
厂商：思科 Small Business（SG350X 等）
凭据类型：保持静态密码
```

只修改厂商字段时应保留原静态密码密文。随后进行一次只读验收：让运维助手查询设备配置，确认：

- 后端日志中的 vendor 为 `cisco_small_business`。
- Netmiko 不再等待包含 `\x1b[K` 的错误提示符。
- `show running-config` 不停在分页提示符。
- Agent 收到配置输出或现有长度上限内的截断输出。

真实设备验收需要用户单独确认后执行；本次实现阶段不主动连接设备。
