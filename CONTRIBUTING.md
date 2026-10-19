# Contributing

Thanks for taking the time to improve this project. 欢迎提交 issue 和 PR。

## 报告问题

提 issue 前请先：

1. 确认微信版本是 **4.1.9.x**（4.1.10+ 拿不到密钥，属于已知限制）
2. 搜索现有 issue，避免重复
3. 附上以下信息（**先删掉敏感内容**）：
   - Windows 版本、微信版本、Python 版本
   - `tmp/bridge.log` 里相关的几行（删除 wxid、联系人名、密钥）
   - 复现步骤和你期望的结果

> ⚠️ **绝对不要**贴 `keys.txt`、解密后的 `.db`、`tmp/wx-images/` 里的原图或完整聊天记录。

## 提交 PR

- 一个 PR 只做一件事，描述里写清楚「为什么」而不只是「改了什么」
- 不要动加密参数、WAL 盐过滤、UI 自动化流程，除非有充分证据
- 新增行为请同时更新 README 的「踩坑清单」或对应文档
- 提交前跑一遍：

```powershell
python -m py_compile tools\wx-bridge.py tools\wx_images.py tools\wx-read.py
```

- 代码风格：Python 4 空格缩进；PowerShell 脚本保持 UTF-8 **带 BOM**；不引入新的大依赖

## 安全问题

请不要在公开 issue 里讨论漏洞或密钥泄露，按 [SECURITY.md](SECURITY.md) 的方式私下报告。
