---
name: file-summarizer
description: 总结一个 Python 文件的代码结构和功能，输出简洁报告
---

# file-summarizer

当你需要快速了解一个 .py 文件的用途时使用此 skill。

## 步骤

1. 用 `glob` 找到目标文件
2. 用 `read` 读取文件（建议 limit=50）
3. 提取：模块用途、主要函数/类、依赖的库
4. 输出格式：
   - **文件名**
   - **用途**：一句话概括
   - **主要组件**：函数/类列表
   - **依赖**：import 的库
