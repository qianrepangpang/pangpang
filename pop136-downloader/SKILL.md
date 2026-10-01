---
name: pop136-downloader
description: "运行、维护和构建 POP136 自动下载器；适用于登录浏览器、断点续传、批量下载、故障恢复和下载性能检查。"
---

# Pop136 Downloader

用于在 Windows 上维护 POP136 自动下载器。源码和测试位于 `scripts/`，不要把构建产物、登录 Cookie、下载状态文件或用户素材提交到仓库。

## 固定行为

- 默认下载目录为 `H:\POP136原图`，登录状态目录为 `H:\POP136浏览器登录状态`。
- 只下载站点允许的非 PSD/EPS 文件；必须保留 `.ai` 文件。
- 浏览器登录和验证码由用户完成；不能绕过权限、购买服务或发布后台商品。
- 单文件总耗时超过 2 分钟时保留 `.part` 断点并跳到下一个文件。
- 使用一个 Chrome 窗口及标签页池，不能反复创建浏览器窗口。
- 下载状态写入目标目录的 `_download_state.json`，日志写入 `_download_log.txt`；重新开始时从断点继续。
- 每页记录处理耗时，便于比较版本性能；修改下载并发或超时后必须运行测试。

## 常用操作

1. 先确认 Chrome 登录状态和目标目录，再运行 `scripts/pop136_app.py` 或构建后的桌面程序。
2. 用户完成网页登录后点击“开始 / 续传”；暂停时保留断点，不删除状态文件。
3. 修复后在 `scripts/` 目录执行：

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   python -m unittest discover -s tests -v
   ```

4. 构建桌面版时使用 `scripts/POP136_Downloader_2_1.spec`，并确认程序标题版本、下载目录和浏览器窗口行为。

## 修改边界

只修改与 POP136 下载器直接相关的源码和测试。不要提交 `dist/`、`build/`、旧版 EXE、登录配置、Cookie、`_download_state.json`、`_download_log.txt` 或下载图片。涉及真实下载时，先检查当前状态并避免覆盖已有文件。
