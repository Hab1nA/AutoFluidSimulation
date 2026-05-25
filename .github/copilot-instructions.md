# Commit 信息规范

使用中文撰写 commit 信息，遵循以下格式：

```
<type>: <description>
```

## Type 类型

- `feat` — 新功能
- `fix` — 修复 bug
- `refactor` — 重构（不改变功能）
- `perf` — 性能优化
- `docs` — 文档变更
- `chore` — 构建/依赖/工具链等杂项
- `test` — 测试相关
- `style` — 代码格式调整（不影响逻辑）

## 要求

- description 简洁扼要，不超过 72 字符
- 使用中文描述改动内容
- 不需要 scope（模块名）
- 示例：`feat: 添加网格划分超时重试机制`
