# F_34 可选本地 context 文件缺失

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | context section 本地缺失文件处理 |
| 测试基线 | 真实 Local SysOperation、临时文件及现有 prompt 回归 |
| Refs | R2-B；未指定 issue |

## 背景

ContextAssembleRail 每次装配调用 build_context_file_sections，默认包含可选 HEARTBEAT.md。
_read_context_file 承诺缺失返回 None，但本地 stamp 将 FileNotFoundError 与其它 OSError
混同为不可缓存，随后仍调用 SysOperation.read_file，导致正常缺失反复生成 ERROR 日志。

## 决策

仅当 OperationMode.LOCAL 的原 Path.stat 明确抛 FileNotFoundError 时安静返回 None。
私有 stamp 保留这个缺失信号，由 reader 捕获；同时移除该路径已有缓存，避免删除后
重建相同 mtime/size 的文件复用旧内容。每次调用仍重新 stat，不缓存永久 missing。
其余 OSError（含权限、I/O、非目录错误）、远端及未知 mode 沿原读取路径；已有文件
仍通过 SysOperation 读取，不变更其授权、日志、锁或错误行为。

## 拒绝的方案

不补 dummy HEARTBEAT.md，不调整日志级别，不用 Path.exists 把未知错误都当缺失，
不让本地缺失决定远端文件是否存在，不扩大资源权限或引入新缓存/状态。

## 验证与遗留

真实本地读组件红测复现三次 ERROR/三次无效读取；修复后缺失不调用 read_file，文件
后来创建被读取。另覆盖删除后相同 stamp 重建、sandbox/未知 mode、本地权限及其它
stat 错误。与附近缓存、prompt、context assemble 用例一并回归。
此为本地可选文件判空修复，不覆盖 stat 与后续读取间的并发删除；该竞态仍由原读取
错误处理。未运行真实 Provider/UI，本地源码结果不替代正式整合后的 strict/按锁验收。
