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

### 3. 创建 PAT 并推送本仓库到 GitHub

**3.1 先确认远端仓库已存在且为空**

1. 打开 https://github.com/new（**先登录你自己的 GitHub 账号**）。
2. **Owner** 保持你自己——页面左上角那个下拉显示的应是你的**登录名**。
   若显示的不是你的账号，先切换账号再建（建到别人名下你推不上去）。
3. **Repository name** 填 `workbuddy`（必须与本地 remote 一致）。
4. **不要**勾 Add a README file / Add .gitignore / Choose a license
   —— 保持空仓库，否则首次 push 会报 `! [rejected] ... (fetch first)`。
5. 点 **Create repository**。创建后地址栏应显示 `github.com/<你的登录名>/workbuddy`，
   **把这个 `<你的登录名>` 记下来**，3.3 配 remote 要用它。

**3.2 创建推送用 PAT（先分辨你在哪个页面）**

看浏览器地址栏：
- `github.com/settings/personal-access-tokens/new` → **细粒度页**，`Repository access` 有**三个**选项（含 `Only select repositories`）
- `github.com/settings/tokens/new` → **经典页**，`Repository access` 只有 `Public repositories` / `All repositories` 两个

> 若你看到只有两个选项，说明这是经典令牌页。经典令牌**无法限定到单个仓库**（只能 All repositories），
> 安全性弱于细粒度。想锁定单仓库请直接用细粒度页地址：https://github.com/settings/personal-access-tokens/new

**A. 细粒度页（推荐）**

1. **Token name** 填 `workbuddy-push`
2. **Expiration** 选 90 天或最长 1 年
3. **Repository access** → 选 **Only select repositories** → 下拉里勾选 **workbuddy**
   （若下拉为空/没有这一项，先回 3.1 把仓库建出来）
4. **Permissions** 区点右侧 **+ Add permissions** → 在弹出的分组里选 **Repository**
5. 勾选下面三行，每行右侧 Access 都设成 **Read and write**：

   | 权限 | 作用 | 不勾的后果 |
   |---|---|---|
   | **Contents** | 推送提交 | push 直接 403 |
   | **Workflows** | 推送 `.github/workflows/*` | **push 被拒**（本仓库含 `daily-checkin.yml`） |
   | **Secrets** | 写仓库 Actions Secret | 后面自续期无法写回（可选但建议一次配齐） |

   `Metadata` 会自动变 Read-only，不用管。Account 分组不用加。

> **清掉无关权限**：`+ Add permissions` 下拉里选项极多，很容易点错。若权限列表里出现
> `Repository creation`、`Repository security advisories`、`GitHub App installation repository access`
> 之类的行，点每行右侧的 **✕** 删掉。最终只该剩 **4 条**：
> `Metadata`(Required) + `Contents` + `Workflows` + `Secrets` —— 加完后 `Repositories` 标签上的计数应显示 **4**。
>
> 别被名字坑：细粒度页里没有 `workflow scope` 这个说法，推 workflow 文件靠的就是权限行 **`Workflows`**
> （描述为 "Add and update GitHub Action workflows"）。它**只能**在这里授予，`permissions:` YAML 里写不了。
> 漏了它的报错是 `refusing to allow a ... to create or update workflow ... without 'workflows' permission`。
6. 拉到底点 **Generate token** → 顶部出现 `github_pat_` 开头的串，**只显示一次**，立即复制。

**B. 经典页**

1. **Repository access** → **All repositories**（经典令牌无法限定单仓库）
2. **Permissions** → **+ Add permissions** → **Repository** → 同样勾 **Contents / Workflows / Secrets** 全部 **Read and write**
3. **Generate token** → 复制 `ghp_` 开头的串

**3.2.1 先确定你的 GitHub 登录名（关键，别用 Windows 用户名）**

**Windows 用户名 ≠ GitHub 用户名**，两者毫无关系，不能想当然。本机 Windows 账户叫 `simona`，
而 GitHub 上的 `Simona` 是 **2010-07-28 注册、2015 年后便无更新、0 个公开仓库的休眠陌生人账号**——
拿它当 owner/用户名，认证必然被拒：

```
remote: Invalid username or token. Password authentication is not supported for Git operations.
fatal: Authentication failed for 'https://github.com/simona/workbuddy.git/'
```

用下面这段一次性查出**你真实的登录名**，同时验证令牌是否可用（PowerShell）：

```powershell
# 本机 Windows 根证书库缺 Sectigo 根（见 3.3.1），.NET 的 HTTPS 同样会失败，故先对本次会话关闭校验
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}
$t = Read-Host "粘贴 PAT（不回显）"
$h = @{ Authorization = "Bearer $t"; "User-Agent" = "wb-checkin" }
try {
    $me = Invoke-RestMethod -Headers $h -Uri "https://api.github.com/user"
    Write-Host ("你的 GitHub 登录名 = " + $me.login)
    $u = $me.login
    try {
        $r = Invoke-RestMethod -Headers $h -Uri "https://api.github.com/repos/$u/workbuddy"
        Write-Host ("仓库已存在：" + $r.full_name)
    } catch {
        Write-Host "仓库 $u/workbuddy 不存在或令牌无权访问 -> 去 https://github.com/new 建空仓库 workbuddy"
    }
} catch {
    Write-Host ("令牌无效 / 已过期 / 复制不全：" + $_.Exception.Message)
}
```

读结果：

| 输出 | 含义 | 下一步 |
|---|---|---|
| `你的 GitHub 登录名 = xxx` | 令牌可用，`xxx` 就是你的真实登录名 | 记下 `xxx`，3.3 里替换所有 `<你的登录名>` |
| `令牌无效 / 已过期 / 复制不全：... 401 ...` | **令牌**问题 | 回 3.2 重新生成，注意整串复制（`github_pat_` 或 `ghp_`） |
| `仓库 xxx/workbuddy 不存在...` | 仓库没建，或建在了别人名下 | 回 3.1，在**你自己的账号**下建空仓库 |
| `Bad credentials` | 同上，令牌或不匹配 | 同上 |

> `ServerCertificateValidationCallback = {$true}` **只影响当前 PowerShell 窗口**，关掉即失效，
> 不写任何系统设置、不改全局信任库。之所以需要它，是因为本机根证书库缺 Sectigo 根；
> 我们已用 `openssl s_client` 抓链确认 GitHub 证书链真实（`Sectigo DV E36 ← Sectigo Root E46 ← USERTrust ECC`），
> 无中间人替换，这才敢临时关校验。

**3.3 推送**

**第一步：让 remote 与用户名对准你自己的账号**（`<你的登录名>` 取 3.2.1 打印的 `login`）

```powershell
git config credential.username "<你的登录名>"      # 预填用户名，避免手打错
git remote set-url origin "https://github.com/<你的登录名>/workbuddy.git"
git remote -v                                      # 核对：两条 URL 都应是 https://github.com/<你的登录名>/workbuddy.git
```

> 已经知道自己的 GitHub 登录名？那就不用跑 3.2.1 的 API 脚本，直接把上面两条命令里的
> `<你的登录名>` 换成它即可。
>
> 可选（纯署名，不影响能否推送）：让提交归属到你的账号——
> ```powershell
> git config user.name "<你的登录名>"
> git config user.email "<你的登录名>@users.noreply.github.com"
> ```

**第二步：选对 shell 再推**

> ⚠️ **先确认自己在哪个 shell 里**。提示符是 `PS C:\...>` 就是 **PowerShell**，是 `C:\...>` 才是 **cmd**。
> 两者语法不通用：`cd /d` 是 **cmd 专有**，在 PowerShell 里会报
> `Set-Location : 找不到接受实际参数"/d"的位置形式参数`。

**PowerShell**（提示符 `PS C:\...>`）：
```powershell
cd "C:\Users\simona\WorkBuddy\2026-09-29-16-30-33\workbuddy-actions-checkin"
git rev-parse --show-toplevel          # 应回显 ...\workbuddy-actions-checkin，确认进对目录
git push -u origin main
```

**cmd**（提示符 `C:\...>`，没有 `PS`）：
```cmd
cd /d C:\Users\simona\WorkBuddy\2026-09-29-16-30-33\workbuddy-actions-checkin
git rev-parse --show-toplevel
git push -u origin main
```

> 仓库级配置已预置（见 3.3.1），所以命令里不需要再带 `-c http.sslBackend=...` / `-c credential.helper=`。
> 若换到别的机器/别的仓库，把参数显式写回去即可：`git -c credential.helper= -c http.sslBackend=openssl push -u origin main`。

> 想用 cmd 但当前在 PowerShell？先敲一个 `cmd` 回车切换，再执行上面的 cmd 版。
> PowerShell 5.1 不支持 `&&` 串联，**一行一条**执行，别粘成一行。

- **必须先 cd 进仓库目录再执行**，且执行前用 `git rev-parse --show-toplevel` 自检。
  在 `C:\Users\simona>` 下执行会报 `fatal: not a git repository`；若该目录还残留 `.git`，
  更会把整个用户主目录当仓库（塞入 `.workbuddy` 运行时缓存，报 `adding files failed`）。
- 提示 `Username for 'https://github.com':` → 已由 `credential.username` 预填，**直接回车**。
  （若仍需手输：填 3.2.1 查到的登录名，**绝对不要填 Windows 用户名 `simona`**）
- 提示 `Password for 'https://<你的登录名>@github.com':` → **粘贴 PAT**（屏幕不回显，正常）
  —— 这里要的是**令牌**（`github_pat_` / `ghp_` 开头），**不是你 GitHub 账号的登录密码**。
- 成功标志：`branch 'main' set up to track 'origin/main'.`

**3.3.1 本机 TLS 环境修复（已预置，无需操作）**

本机曾报 `fatal: unable to access 'https://github.com/...': schannel: SEC_E_UNTRUSTED_ROOT (0x80090325)`。
根因**不在 GitHub、不在令牌、不在代理**，而是：

- Windows **根证书库缺少 Sectigo / USERTrust 根证书**（`certutil -store Root` 共 377 条，
  含 DigiCert 等，唯独无这张），schannel 无法为 GitHub 的
  `github.com ← Sectigo DV E36 ← Sectigo Root E46 ← USERTrust ECC` 链找到信任锚，
  于是握手阶段直接失败。根证书自动更新通道亦不可用，无法自愈。
- 附带故障：PortableGit 的 `credential.helper=helper-selector` 同样走 schannel，
  会额外抛 `未能为 SSL/TLS 安全通道建立信任关系`（中文 .NET 措辞），属**同源问题**。

已在本仓库写入两条**仓库级**配置（不动全局，避免影响内网 Git 服务器）：

```ini
http.sslBackend = openssl      # 改用 git 自带的 Mozilla CA 包，绕开 Windows 根证书库
credential.helper =            # 置空 = 禁用 helper-selector，改为终端直接输入账号密码
```

验证：`git ls-remote origin --heads` 的报错若从 `schannel: SEC_E_UNTRUSTED_ROOT`
变成 `could not read Username ...`，即 **TLS 已通、只差凭据**。

> 换新机器或新 clone 后又出现该错误，在新仓库里重跑一次即可：
> ```bash
> git config http.sslBackend openssl
> git config credential.helper ""
> ```
> 若想让全局的 GitHub 仓库都生效（**注意**：会让内网自签 CA 的 Git 服务器验证失败，
> 除非另配 `http.sslCAInfo`）：`git config --global http.sslBackend openssl`。

验证：
```bash
git ls-remote origin --heads
```
出现 `refs/heads/main` + SHA 即成功。

可选——推送前先验一下这个 PAT 到底能不能看到该仓库（200 = 正常，401 = 令牌错，404 = 没勾选这个仓库）：
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer <粘贴你的PAT>" https://api.github.com/repos/<你的登录名>/workbuddy
```
（PowerShell 里可用 `curl.exe` 以避免 `curl` 别名冲突；cmd 里把 `/dev/null` 换成 `NUL`。
注意：本机 `curl` 也用 schannel，做这个探测需加 `--ssl-no-revoke` 或改用 `curl.exe --insecure`；
更稳的做法是直接用 `git ls-remote` 验证。）

**备选：交互式提示不出现时**（终端不弹账号密码，卡住或直接 403）。用一次性 URL 认证，
令牌**只存在于当前会话变量**、不落 PowerShell 历史（`Read-Host` 不回显），用完立刻把 remote 复原：

```powershell
$u = "<你的登录名>"
$t = Read-Host "粘贴 PAT"
git remote set-url origin "https://$u`:$t@github.com/$u/workbuddy.git"   # `:` 反引号转义，否则 PS 会当成作用域
git push -u origin main
git remote set-url origin "https://github.com/$u/workbuddy.git"          # 立刻复原，避免令牌留在 .git/config
Remove-Variable t, u
```
> 不要在双引号里漏掉 `` ` ``：PowerShell 会把 `$u:` 解析成"变量 u 的 scope"，
> 写成 `"$u:$t@..."` 会得到空值并报奇怪的错。上例已处理。
> 不要直接把 `https://user:token@...` 明文敲进命令行——PowerShell 会把明文存进
> `ConsoleHost_history.txt`。上面这种写法不会。

**3.4 常见拒绝与处置**

| 报错 | 原因 | 处置 |
|---|---|---|
| `remote: Invalid username or token. Password authentication is not supported for Git operations.` | ① **用户名不是你的 GitHub 登录名**（最典型：填了 Windows 用户名 `simona`）；② 在 Password 处填了账号**密码**而非 PAT；③ PAT 复制不全 / 已过期 / 已被撤销 | 先跑 **3.2.1** 查出真实登录名并对齐 remote；确认粘贴的是 `github_pat_`/`ghp_` 令牌 |
| `fatal: not a git repository (or any of the parent directories): .git` | 当前目录不是仓库（多半还在 `C:\Users\simona`） | `cd` 到仓库目录，用 `git rev-parse --show-toplevel` 自检后再 push |
| `Set-Location : 找不到接受实际参数"/d"的位置形式参数` | 在 **PowerShell** 里用了 **cmd** 的 `cd /d` | 去掉 `/d`：`cd "完整路径"`；或先敲 `cmd` 切到 cmd |
| `schannel: SEC_E_UNTRUSTED_ROOT (0x80090325)` | Windows 根证书库缺 Sectigo/USERTrust 根证书 | 见 3.3.1，仓库级 `git config http.sslBackend openssl` |
| `未能为 SSL/TLS 安全通道建立信任关系` | 同上的根证书问题，但由 `helper-selector` 抛出 | 见 3.3.1，`git config credential.helper ""` |
| `refusing to allow a Personal Access Token to create or update workflow ...` | PAT 缺 Workflows 权限 | 回 3.2 补 **Workflows: Read and write**，重新生成令牌再 push |
| `! [rejected] main -> main (fetch first)` | 远端非空（建仓时勾了 README） | `git pull --rebase origin main` 后重试；**不要加 `--force`** |
| `Authentication failed` | PAT 复制不全 / 已过期 | 重新生成 |
| `remote: Write access to repository not granted` | PAT 未勾选该仓库 | 回 3.2 第 3 步把 `workbuddy` 勾上 |
| `remote: Repository not found` | 远端仓库还没建；仓库名/owner 拼错；或仓库建在了**别人的账号**下 | 回 3.1 在你自己账号下建仓；核对 `git remote -v` 的 owner 是否等于 3.2.1 查到的登录名 |

### 4.（可选）云端自续期 —— 实现「本机永久关机也能跑」
长效令牌本身只有 ~30 天有效期。默认模式下需每 ~30 天开一次本机重注入；
启用自续期后，云端每次刷新把新 refreshToken 写回 Secret，从此本机只需最初注入一次。
唯一仍需偶尔操作的，是 `WB_GH_PAT` 这个令牌本身约 1 年到期（到期前在**任意浏览器**续期一次即可，**不依赖本机客户端**）。

**最省事的做法：直接复用 3.2 建的 PAT。** 只要 3.2 第 5 步勾了 **Secrets: Read and write**，
把它原样填进 `WB_GH_PAT` 即可，不用再建第二个令牌。

若要单独建（截图级点击路径）：

1. 点 GitHub 右上角头像 → **Settings**。
2. 左侧边栏底部点 **Developer settings**。
3. 点 **Personal access tokens** → **Fine-grained tokens**。
4. 点绿色按钮 **Generate new token**。
5. **Token name** 填 `workbuddy-checkin-renew`（便于识别）。
6. **Expiration** 选最长（细粒度最多 1 年）；到期前续期一次即可。
7. **Resource owner** 选你自己的账号（或拥有该仓库的 org；选 org 可能需管理员审批）。
8. **Repository access** 选 **Only select repositories** → 在下拉里勾选 **workbuddy**
   （若只有 Public/All 两个选项，说明进的是经典令牌页，改用 https://github.com/settings/personal-access-tokens/new）。
9. **Permissions** → 点 **+ Add permissions** → 选 **Repository** 分组 →
   找到 **Secrets** → Access 设 **Read and write**。
   —— 写 Actions Secret 靠这条；**不是** Administration，也不是 Actions。`Metadata` 会自动勾选，不用管。
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
