# WorkBuddy 每日签到 + 派猫猫旅行 · GitHub Actions 自动化

把「Buddy 加油站」每日签到和「派猫猫旅行」用 GitHub 免费定时任务跑起来，结果推送到飞书。
**全程云端执行，不依赖你本机常开**；云端无登录态，令牌只从仓库 Secret 读取。

## 架构

```
本机（一次性，仅最初）         GitHub 仓库（常驻云端）
──────────────────            ──────────────────────
refresh_token_local.py  ──▶   Secrets: WB_REFRESH_TOKEN
 读本机登录态导出令牌            Secrets: WB_FEISHU_WEBHOOK / WB_FEISHU_SECRET
 注入仓库 Secret                Secrets: WB_GH_PAT（可选，用于自续期）

                                 │
                          GitHub Actions (schedule 每日 09:00 北京)
                                 │
                          scripts/run_checkin.py
                            1. 用 WB_REFRESH_TOKEN 换接口令牌（插件网关刷新接口）
                            2. 签到：状态查询 → 领取（幂等跳过已签）
                            3. 派猫猫：先领到家的奖励 → 再判断是否派新一趟
                               （traveling / 每日上限则跳过派遣）
                            4. 飞书推送（签到 / 猫猫 分两段写清楚）
                            5. 签到成功即整体成功，猫猫失败不影响结论
                            6.（可选）把刷新得到的新 refreshToken 写回 Secret → 自续期
```

> **长效令牌有效期实测为 ~30 天**（`refreshExpiresIn: 2592000`）。
> 不启用自续期 → 每 ~30 天需开一次本机重注入；启用自续期（见下）→ 本机只需最初注入一次。

## 部署前准备清单（软件客户端与环境）

本方案的**日常运行全在 GitHub 云端**，只有**最初那一次**需要本机开机：从已登录的 WorkBuddy 客户端登录态解密出长效令牌并注入仓库 Secret（令牌只能从已登录客户端得到，硬约束，代码绕不过）。明确这一点，就能理解「本机关机不影响云端定时运行」——前提是已经完成了那唯一一次本机令牌提取。

### 本机需准备的软件 / 账号（一次性）

| 软件 / 账号 | 在本方案中的角色 | 必装？ | 获取 / 验证 |
|---|---|---|---|
| **WorkBuddy 客户端**（你正在用的工具） | 已登录运行，供 `rt_auth.py` 读取本地登录态、解密出长效令牌——令牌的唯一来源 | 必装（你已有） | 保持登录、未退出即可 |
| **totorosir-workbuddy-checkin 技能** | `refresh_token_local.py` 复用其中 `rt_auth.py` 读登录态 | 必装（你已装） | 已存在 `~/.workbuddy/skills/totorosir-workbuddy-checkin__skillhub/scripts/rt_auth.py` |
| **Python 3.8+** | 运行本机导出/注入脚本 | 必装 | 见下方安装 |
| **Git** | 初始化并推送仓库 | 必装 | 见下方安装 |
| **GitHub CLI（gh）**（推荐） | 一条命令写入仓库 Secret，免去手动复制 | 强烈推荐 | 见下方安装 |
| **GitHub 账号** | 存放仓库、承载 Actions | 必装 | 免费注册 github.com |
| **飞书账号 + 群机器人** | 接收每日推送 | 必装 | 见下方「一次性配置 → 步骤 1」 |

> 注意：云端 `run_checkin.py` 是**自包含**的——只读 Secret 令牌、直接打真实接口，不依赖 WorkBuddy 客户端、不依赖上面那个技能、也不需要本机有 PyNaCl（自续期用的 PyNaCl 由 workflow 自动 `pip install`）。所以「本机关机」只影响最初那一次令牌提取，不影响后续任何云端运行。

### 各软件安装与验证（Windows）

所有命令在 Git Bash / PowerShell / CMD 均可；路径用正斜杠。

**(1) Python 3.8+**
- 下载：python.org 或 Microsoft Store 搜「Python」；安装时务必勾选 **Add Python to PATH**。
- 验证（装完重开终端）：
  ```bash
  python3 --version    # Windows 多为 python，下文同理
  ```
  本方案脚本已兼容 `python3` 与 `python`；若系统只有 `python`，下文命令把 `python3` 换成 `python`。

**(2) Git for Windows**
- 下载：git-scm.com → Download；安装向导 **Select Components** 勾选「Git from the command line...」。
- 验证：`git --version`
- 首次设置身份（推仓库需要）：
  ```bash
  git config --global user.name  "你的名字"
  git config --global user.email "你的邮箱"
  ```

**(3) GitHub CLI（gh，推荐）**
- 下载：cli.github.com 或 `winget install --id GitHub.cli`。
- 登录（弹浏览器授权）：`gh auth login` → 选 GitHub.com → 浏览器授权 → 默认范围含 repo。
- 验证：`gh auth status` 显示 `Logged in to github.com` 即就绪（注入 Secret 的 `gh secret set ... -R <repo>` 才能免密执行）。

**(4) WorkBuddy 客户端 + 签到技能（确认而非安装）**
- 客户端保持**已登录**（最近登录过、未退出）。
- 确认技能脚本存在（导令牌要用）：
  ```bash
  ls "$HOME/.workbuddy/skills/totorosir-workbuddy-checkin__skillhub/scripts/rt_auth.py"
  ```
  存在即可；若技能目录改名，脚本会用 glob `*workbuddy-checkin*` 自动匹配。

### 部署后可在纯浏览器完成的事项（不再依赖本机）

| 操作 | 在哪里做 | 需本机开机？ |
|---|---|---|
| 推仓库（git push） | 本机 Git，或 GitHub 网页 Upload | 推仓库时需本机（也可网页上传） |
| 设 `WB_REFRESH_TOKEN` / 飞书凭据 | `refresh_token_local.py`（本机）或 GitHub UI（浏览器） | 首次**提取令牌**需本机；之后无需 |
| 设 `WB_GH_PAT`（自续期用） | 浏览器 GitHub 网页 | 否 |
| 首次触发 / 看运行日志 | 浏览器 GitHub Actions 页 | 否 |
| 每日定时运行 | GitHub 云端 runner | 否 |
| `WB_GH_PAT` 续期（约 1 年一次） | 浏览器 GitHub 网页 | 否 |

软件客户端就绪后，按下方「一次性配置」的步骤 1–4 完成飞书机器人、注入、推送与（可选）自续期即可。

## 接口（以实测为准，不用旧域名/旧路径）

| 用途 | 方法 | 实际地址 |
|---|---|---|
| 长效令牌刷新 | POST | `https://copilot.tencent.com/v2/plugin/auth/token/refresh` |
| 签到状态 | POST | `https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status` |
| 签到领取 | POST | `https://www.codebuddy.cn/v2/billing/meter/daily-checkin` |
| 旅行状态 | GET | `https://www.workbuddy.cn/activity/growth/buddy/travel/status` |
| 旅行领取 | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/claim` |
| 旅行派遣 | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/depart` |

> 旅行接口域名是 `www.workbuddy.cn`（**不是**登录态里的 `www.codebuddy.cn`），且**不带 `/v2` 前缀**——这是最常见的 404 坑。刷新接口不校验 User-Agent，天然适配云端定时场景。

## 一次性配置

### 1. 准备飞书群机器人
飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 拿到 Webhook 地址。
（可选）开启「签名校验」拿到加签密钥。

### 2. 注入密钥到仓库 Secret

**方式 A（推荐，一条命令全搞定）** —— 在你的电脑上（WorkBuddy 客户端已登录运行）执行，
脚本会把 `WB_REFRESH_TOKEN` **以及**飞书两条凭据一次性注入仓库 Secret：

```bash
python3 scripts/refresh_token_local.py --repo <你的owner>/<仓库名> \
    --feishu-webhook "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" \
    --feishu-secret "你的加签密钥（没开签名校验可省略此参数）"
```

- 脚本复用已安装的 `totorosir-workbuddy-checkin` 技能导出长效令牌，自动处理 5.6.2+ 加密登录态。
- 明文令牌只经 `gh secret set -b` / HTTPS 直传，**不写文件、不进日志**；终端只打印脱敏串。
- 飞书两条凭据**用方式 A 已自动写入**，无需再到 UI 手动加；只有下面的情况才走方式 B。

**方式 B（手动在 GitHub UI 加，截图级）** —— 适用于：没装 `gh`/`GH_TOKEN`、或只想单独补某条 Secret。

1. 打开仓库首页 → 点顶部导航栏的 **Settings**（仓库级 Settings，不是个人头像里的 Settings）。
2. 左侧边栏最底部点 **Secrets and variables** → 展开后点 **Actions**。
3. 进入 *Actions secrets* 页，点右上角绿色按钮 **New repository secret**。
4. **Name** 填 `WB_FEISHU_WEBHOOK`；**Secret** 文本框粘贴飞书机器人完整 Webhook
   （形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`）→ 点 **Add secret**。
5. 重复第 3–4 步，新增 `WB_FEISHU_SECRET`（值为开启「签名校验」时拿到的加签密钥；
   **没开签名校验则不用加这条**）。
6. （若没用方式 A）同样新增 `WB_REFRESH_TOKEN`，值来自本机 `python3 scripts/refresh_token_local.py --check`
   打印的明文——**仅此一次需要本机在线**。
7. 保存后列表应出现对应条目且有 *Last updated* 日期。GitHub 会自动在日志掩码 Secret，无需手动处理。

> 无论方式 A/B，三条 Secret 为：`WB_REFRESH_TOKEN`（必填）、`WB_FEISHU_WEBHOOK`（必填）、
> `WB_FEISHU_SECRET`（可选，取决于飞书是否开签名校验）。

### 3. 推送本仓库到 GitHub
```bash
git init && git add -A && git commit -m "add workbuddy checkin actions"
git remote add origin git@github.com:<owner>/<仓库名>.git
git push -u origin main
```

### 4.（可选）云端自续期 —— 实现「本机永久关机也能跑」
长效令牌本身只有 ~30 天有效期。默认模式下需每 ~30 天开一次本机重注入；
启用自续期后，云端每次刷新把新 refreshToken 写回 Secret，从此本机只需最初注入一次。
唯一仍需偶尔操作的，是 `WB_GH_PAT` 这个令牌本身约 1 年到期（到期前在**任意浏览器**续期一次即可，**不依赖本机客户端**）。

**新建细粒度 PAT（截图级点击路径）：**

1. 点 GitHub 右上角头像 → **Settings**。
2. 左侧边栏底部点 **Developer settings**。
3. 点 **Personal access tokens** → **Fine-grained tokens**。
4. 点绿色按钮 **Generate new token**。
5. **Token name** 填 `workbuddy-checkin-renew`（便于识别）。
6. **Expiration** 选最长（细粒度最多 1 年）；到期前续期一次即可。
7. **Resource owner** 选你自己的账号（或拥有该仓库的 org；选 org 可能需管理员审批）。
8. **Repository access** 选 **Only select repositories** → 在下拉里勾选你的签到仓库（如 `workbuddy-actions-checkin`）。
9. 展开 **Repository permissions** → 找到 **Secrets (repo)** → 权限选 **Read and write**
   （写 Actions Secret 靠这条；**不是** Administration，也不是 Actions）。Metadata 会自动勾选，不用管。
10. 页面底部点 **Generate token**。
11. 生成的令牌以 `github_pat_` 开头，**只显示一次**，立即复制。
12. 回到仓库 **Settings → Secrets and variables → Actions → New repository secret**，
    **Name** 填 `WB_GH_PAT`，**Secret** 粘贴该令牌 → **Add secret**。

Workflow 检测到 `WB_GH_PAT` 后会自动 `pip install pynacl` 并把新 refreshToken 写回 `WB_REFRESH_TOKEN`。

> 安全说明：`WB_GH_PAT` 具备写本仓库 Actions Secret 的能力，务必用「仅限本仓库」的细粒度令牌
> （第 8 步选 Only select repositories），不要用有整账号 `repo` 全权的经典 PAT。
> 它也只存在于 GitHub Secret，不进本仓库/日志。

## 验证 / 跑通一次

- **本地不联网预演**（看输出与飞书文案格式，无凭据也能跑）：
  ```bash
  python3 scripts/run_checkin.py --dry-run
  ```
- **真实跑一次**：在 GitHub 仓库 Actions 页点「Run workflow」，或：
  ```bash
  gh workflow run daily-checkin.yml
  ```
  首次真实执行建议在 WorkBuddy 客户端已登录当天、且你还没手动签到时触发，
  便于到「Buddy 加油站」核对 +100。已签会自动幂等跳过。

## 续期
- **已启用自续期（配了 `WB_GH_PAT`）**：云端每日刷新自动写回新令牌，无需任何人工操作。
  唯一失效场景是 `WB_GH_PAT` 本身过期/被撤销——届时飞书报「写回失败」，更新该 PAT 即可。
- **未启用自续期**：长效令牌 ~30 天有效。过期后飞书会收到「刷新失败」，重新在本机跑一次
  第 2 步的 `refresh_token_local.py` 注入最新令牌即可，无需改代码。

## 关键约束落实
- ✅ 云端无登录态：`run_checkin.py` 只读 `WB_REFRESH_TOKEN` Secret，绝不读本机文件。
- ✅ 明文 token 不进仓库/日志：GitHub 自动掩码 Secret；脚本所有输出对 token 打码。
- ✅ 解释器不写死版本：`workflow` 用 `command -v python3 || command -v python`。
- ✅ 接口名以实测为准：见上表，未使用任何旧域名/旧路径。
- ✅ 先领后派、今日已派不重复派：`run_travel` 严格按 `state/idle + daily_limit_reached` 判定。
- ✅ 飞书分段 + 猫猫失败不影响结论：签到成功即退出码 0。
