# F_36 原 Round STEER 最终准入

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-04 |
| 范围 | DeepAgent 私有原 Round 补充输入边界 |
| 测试基线 | 隔离确定性组件；不执行真实 Provider 对抗 |
| Refs | R1-13C / R2-B3/B4；S_03 |

## 背景与决策

宿主 dispatch guard 之后仍有 send/control 两次锁等待，普通STEER可以在原Round结束后
转为新Round。因此只在宿主await前后查权限不足以阻止临时输入凭据失效后的入队。
新增私有原Round端口，同原source保留父凭据，同时在锁内实际enqueue前检查本次输入。
复用原controller/队列/锁，不新增状态机、协议字段、持久权限或输出消费路径。

## 拒绝的方案

不把临时checker塞进inputs或wire，不用新ExecutionOrigin替换父source，不在宿主直接
访问loop队列或借latest Round，不允许空闲fallback，也不静默改变普通send_input兼容。

## 验证与已知遗留

新增锁等待后的凭据/原Round替换、输入漂移、waiting/idle/已结束以及正常原STEER测试；
结果在完成后补记。宿主opaque selector与Runtime原input handle接线仍需独立集成及真实
普通UI验收；此端口不代表Goal、后台子执行或真实撤权已完成。


最终受影响69项通过（其中30项新增；9条既有模型警告），2秒内完成。独立审查先复现
8项原引用漂移/最后checker结构变化/已取消facade反例（全为未拒绝断言失败），修正后
同8项与最初22项全部通过。新增正式回归不按checker调用次数硬编码，直接在实际
control锁内注入同步变化，核原队列不接纳。证据本任务`/tmp/r2b-owned-steer-review`与
`/tmp/r2b-owned-steer-final-affected.log`；首轮单一失败为测试误用drain_steering签名，已保留。
完整stable和正式发布配对另行记录，未把组件结果当真实撤权通过。
