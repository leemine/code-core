# F_46 OpenCode 宿主与实例默认 direct

## 元信息

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-10 |
| 范围 | OpenCode默认配置、显式systemd兼容与测试 |
| 测试基线 | 299项OpenCode单测；默认direct真实CLI 4项、显式systemd真实CLI 6项 |
| Refs | F_45、S_19 |

## 背景

显式direct已经支持非root Linux宿主与实例，但遗漏配置时仍要求systemd用户服务。
统一默认值，让相同Provider配置在这两种部署下选择同一启动路径。

## 数据与状态机

`OpenCodeHarnessConfig.server_mode` 从systemd默认改为direct。
公共dataclass、from_mapping和Provider工厂共用这个默认值；SerializedTurnHarness不变。
显式systemd仍可选择，旧systemd身份继续省略模式字段，direct身份继续包含direct。
模式scope保持分离，不自动接管另一模式服务，不改变既有checkpoint恢复检查。

## 决策

默认direct只拥有原生服务子进程，正常abort/idle/stop流程沿用F_45。
宿主自行提供所需OS隔离；默认值本身不提供沙箱，不保证异常工具后代回收。
显式systemd保留cgroup机制。升级前对原来依赖默认值的systemd部署固定
`server_mode: systemd`；不要直接把旧历史的Provider配置改为direct。
现有显式direct的demo配置、存储与历史不受默认值变化影响。

## 拒绝的方案

不探测环境后自动降级，不删除systemd，不新增subreaper，不迁移或复制旧运行目录，
不由Swarm再定义一个Provider默认值。Core合入远端后才升级Swarm锁定依赖。

## 验证

本地299项OpenCode单测、默认direct真实CLI 4项、显式systemd真实CLI 6项均通过；
真实CLI使用回环模型夹具，不计作远程模型验收。定向测试覆盖工厂默认direct、显式systemd旧身份与模式分离、正常关闭与拒绝接管。
原systemd单测和真实CLI夹具显式选择systemd，避免默认变更使原覆盖悄然失效。
direct真实CLI用例取公共默认值，覆盖工具、活动取消、停止、恢复和遗留owner拒绝。
本次是本地提交准备；stable、真实Web模型与新锁定配对属于后续交付关口。

## 已知遗留

Windows/macOS、单Provider异常后代回收不在本次范围；旧隐式systemd部署需要
显式固定配置后升级，不能将默认切换描述为旧会话的无感迁移。
