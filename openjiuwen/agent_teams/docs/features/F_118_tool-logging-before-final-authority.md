# Team 工具日志与最终授权边界

- 日期：2026-10-04
- 范围：tools/tool_base.py、tools/tool_factory.py
- 关联 spec：S_08_team-tools-contract.md
- Refs：#27

## 背景与决策

工厂原来在 Tool 构造后替换 invoke，导致最终授权拒绝全部常规 Team 工具。
TeamTool.__init_subclass__ 只装配子类自己声明的 invoke；继承原方法不重复包装。
工厂私有兼容 helper 对 TeamTool 只开启实例开关，因此重复配置幂等，构造登记的
真实 bound invoke 保持稳定。原 debug 起止消息与结构化返回保持；强制拒绝不执行
日志或工具主体。异常与取消仍传播，不额外记录成功结束。

## 拒绝的方案

拒绝开放任意 invoke 包装器、重登记或哈希白名单，也不删除既有诊断信息。
不把 Team 业务工具挪到公共 core 日志实现中。独立 Swarmflow/AsyncTool 保持旧路径，
该范围尚未接最终 authority，受保护调用会拒绝；此次不扩展 Swarmflow。

## 验证与遗留

真实工厂 AsyncTasksListTool 通过 AbilityManager 验证 allow/deny/legacy、诊断原文、
structured result 与 model rendering；现有 Team 工厂/dispatch variants 回归。
测试清单与确切 SHA 结果由候选证据归档；不能替代 Team UI/API/Provider 验收。
