# 沟通后跳转聊天页导致投递超时

本 fork 基于上游 v0.6.8，修复版版本号为 0.6.9，已包含在 `main` 分支。

## 问题

点击「立即沟通」后，BOSS 有时直接跳转到 `/web/geek/chat`。原版只等待「留在此页」弹窗，失败后仍继续按旧序号读取职位卡片，最终报错：

```text
Locator.get_attribute: Timeout 30000ms exceeded.
waiting for locator("li.job-card-box, .job-card-wrapper").nth(...)
```

## 修复

- 沟通后同时等待确认弹窗或进入聊天页；无法确认的操作保留失败提示。
- 跳转后返回原搜索地址，保留城市、关键词和筛选条件，重新获取职位列表。
- 已处理岗位按职位 ID 去重，恢复后列表重排也不会重复投递。
- 恢复后重新计算下滑次数，避免重复加载首屏导致后续页被提前跳过；投递停顿和分批休息继续生效。
- 列表缩短时停止读取失效序号，读取卡片的超时限制为 2 秒并处理异常。
- 恢复遇到登录、安全验证或列表加载失败时停止当前搜索，保留已完成的岗位记录。

## 验证

完整测试集通过 191 项测试，修改文件通过 Ruff 和 Pyright 检查。

Chrome 浏览器测试使用本机拦截的模拟页面，覆盖直接跳转、正常确认弹窗及每日上限，不发送真实消息。同一直接跳转测试在上游 v0.6.8 可复现原来的 30 秒卡片读取超时，修复后通过。另有单元测试覆盖列表重排、第二页恢复、卡片消失及停止操作。

## 安装 Windows 修复版

Windows x64 的 `.exe` 和 `.msi` 安装包已通过 [GitHub Actions 构建与检查](https://github.com/cg689/ghost-job/actions/runs/38192299052)。在构建页面的 Artifacts 中下载 `ghost-job-windows-x86_64`，解压后任选一种安装包安装。构建产物保留 14 天，也可以在本仓库的 Release CI 工作流中选择 `windows` 重新打包。

安装前关闭正在运行的 Ghost Job。安装后界面版本应显示为 0.6.9，运行需要本机已安装 Google Chrome。

## 使用修复版源码

安装 Git 和 uv 后，在 PowerShell 运行：

```powershell
git clone https://github.com/cg689/ghost-job.git
cd ghost-job
uv sync
uv run python -m job
```

从已有的本 fork 源码更新时，在仓库内运行 `git pull --ff-only`，然后重新启动程序。其他运行准备和配置请参考 [README](README.md)。
