# ArcReel — Fork 工作流

这是 [ArcReel](https://github.com/ArcReel/ArcReel) 的个人 fork，用于维护自定义修改（`ark-seedance-http` 端点等）。

**上游项目规范**：参见 `AGENTS.md` 和 `CONTRIBUTING.md`。

## Git Remote 拓扑

| remote | URL | 用途 |
|--------|-----|------|
| `origin` | `git@github.com:ArcReel/ArcReel.git` | 上游主仓库 |
| `fork` | `git@github.com:BmobSnail/ArcReel.git` | 个人 fork |

当前开发分支：**`em-develop`**（基于 upstream `main` + 自定义修改）。

## 工作流命令

### 同步上游

当用户说 **同步上游** / **sync upstream** / **pull 最新代码** 时：

```bash
# 1. 暂存本地修改
git stash

# 2. 拉取上游 main
git fetch origin
git checkout main
git merge origin/main

# 3. 变基 em-develop
git checkout em-develop
git rebase main

# 4. 恢复本地修改（如有冲突，手动解决后 git add + git rebase --continue）
git stash pop

# 5. 推送到 fork
git push fork em-develop --force-with-lease
```

> **冲突解决**：rebase 冲突时，优先保留 em-develop 的修改（ark_http.py、endpoints.py 注册、i18n 标签）。解决后 `git add <file>` → `git rebase --continue`。

### 本地提交

当用户说 **提交** / **commit** / **推送** / **合入** 时：

```bash
# 1. 在 em-develop 分支上提交（使用 gitlab-commit-message 模板）
git add <files>
git commit -F <临时commit message文件>

# 2. 推送到个人 fork
git push fork em-develop
```

> **不要**直接 push 到 `origin`（上游），只 push 到 `fork`。

### 提交信息模板

使用 `gitlab-commit-message` 模板格式：

```
[类型] 一句话摘要

[why] 为什么改
[how] 怎么改的，后续维护要点
[influence] 受影响模块
[check] 可复现的验证步骤

[jira] NA
```

类型：`bugfix` | `feature` | `improve` | `refactor` | `docs` | `test` | `ci` | `sonar`

## 当前自定义修改

| 文件 | 说明 |
|------|------|
| `lib/video_backends/ark_http.py` | 纯 httpx 视频后端，不依赖 Ark SDK |
| `lib/custom_provider/endpoints.py` | 注册 `ark-seedance-http` 端点 |
| `frontend/src/i18n/{en,zh,vi}/dashboard.ts` | 端点 UI 标签 |

## 本地配置

- **项目路径**：`/Users/cvte/Documents/AI/ArcReel`
- **数据库**：`projects/.arcreel.db`
- **启动**：`scripts/start.sh`
- **停止**：`scripts/stop.sh`
- **llm-api.net token**：`sk-1b5VMuHiSn9KTTdKLHlgdUEsvDRIT2ntJV0Fuz0b1dn7zRbZ`
