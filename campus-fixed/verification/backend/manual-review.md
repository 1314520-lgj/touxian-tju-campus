# 后端优化一：人工审核关卡

自动提取统一生成 pending / review_required=true；只在人工 review(accept) 成功后发出 candidate.accepted。发布入口和旧版本乱序处理要求存在同一候选、同一修订的 accept 审核记录，阻止遗留自动接受任务发布。内部提取器升至 campus-rules-1.4，显式重新提取旧规则候选时重新进入审核；没有批量修改或重新提取随包数据。

修改：content.py、events.py；新增 test_manual_review_gate.py（9 个实例），并把 4 个既有测试文件中的自动接受准备步骤改为显式审核或待审历史校验。模拟旧版错误的人群判断仍保留旧审核记录，当前证据校验没有放宽。

先复现：8 个自动发布/伪接受案例失败，另 1 个未审核新版本覆盖人工决策的用例失败。修复后完整测试 541 passed、8 skipped、0 failed、106 subtests passed（37.62 秒）；基线 532 passed、8 skipped。日志随附。

未验证：隔离 PostgreSQL 环境未配置，8 项保持跳过；真实生产队列未运行。现有公开事项保持原样。带有旧自动接受记录的未处理任务会拒绝发布，需按现有审核入口处理。
