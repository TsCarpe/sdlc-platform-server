# 推送仓库到内网 GitLab 操作指南

> 适用场景：把本地已有仓库（origin 在 GitHub 等）推到公司内网 GitLab 做双远程维护。
> 首次实录：2026-10-30 `sdlc-platform-server` 推送（含一次 HTTP 500 排障），见文末。

## 环境事实（实测）

| 项 | 值 |
|---|---|
| GitLab 地址 | `http://10.30.5.8`（CE 16.5.8） |
| 认证方式 | 仅 HTTP（SSH 22 端口探测关闭）；用户名 + 登录密码或 personal access token |
| 凭据存储 | macOS osxkeychain，首次输入后免密 |
| group 下建项目 | 不支持 push 自动建仓，必须先在网页上建空项目 |

## 操作三步

### 第 1 步：网页建空项目

打开 `http://10.30.5.8/projects/new`：

- **Project URL** 选目标 group（如 `sdlc`），**Project name** 填仓库名
- Visibility Level：Private（默认）
- ⚠️ **不要勾选 "Initialize repository with a README"** —— 保持空仓，否则首推要先 pull 合并

### 第 2 步：本地推送（在对应仓库根目录执行）

```bash
# 1. 新增 gitlab remote（保留原 origin 不动）
git remote add gitlab http://10.30.5.8/<group>/<项目名>.git

# 2. 调大 postBuffer（必做，原因见排障表第 1 条；仓库级配置，设一次即可）
git config http.postBuffer 524288000

# 3. 首次推送——会提示输用户名密码，输完进钥匙串
git push -u gitlab main

# 4. 其余分支按需逐个推（或 git push gitlab --all）
git push -u gitlab <分支名>
```

首次认证：用户名 = GitLab 账号（如 `tsod`），密码 = 登录密码或 personal access token。
⚠️ 首次 push 需在**能交互的终端**里跑（密码提示）；非交互环境会直接报 `terminal prompts disabled`。

### 第 3 步：核对

```bash
git ls-remote gitlab          # 应列出刚推的分支，SHA 与本地一致
git rev-parse main            # 与上一条 refs/heads/main 对比
```

## 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `RPC failed; HTTP 500` + `unexpected disconnect while reading sideband packet` | 整仓首推的 pack 超过默认 1MB postBuffer，chunked 传输被服务端断连 | `git config http.postBuffer 524288000` 后重推（本次实录踩过） |
| `terminal prompts disabled` | 在非交互环境跑首次 push，钥匙串里还没有凭据 | 换系统终端跑一次首次 push，之后恢复免密 |
| 网页建项目时没有目标 group 可选 / 403 | group 下建项目需 Maintainer 及以上权限 | 找 group owner 提权或代建空项目 |
| 密码改过 / 输错过导致 401 | 钥匙串里存了旧凭据 | 钥匙串访问.app 里删掉 `10.30.5.8` 条目重推，或 `printf "protocol=http\nhost=10.30.5.8\n" \| git credential-osxkeychain erase` |
| 默认分支不是 main | GitLab 取首推分支为默认分支；若先推了别的分支 | 项目 Settings → Repository → Default branch 改为 main |

## 日常双 remote 用法

```bash
git push                    # 推 origin（GitHub，照旧）
git push gitlab main        # 推内网
git fetch gitlab            # 拉内网更新（如同事在内网提交后）
```

两 remote 完全独立，分支各自 `-u` 跟踪（`main` 跟 origin、可用 `git push gitlab main` 显式指远端）。

## 首次实录（2026-10-30，sdlc-platform-server）

- 12M 仓库 / 1616 对象，main + mysql-switch 两分支，无 tags
- 首推 main 即中 HTTP 500（postBuffer 1MB 不够），调至 500MB 后一次成功
- mysql-switch 随后直接成功（增量推送不受 postBuffer 影响，但配置已固化无需关心）
- `ls-remote` 核对两分支 SHA 与本地逐一相符，默认分支 = main
