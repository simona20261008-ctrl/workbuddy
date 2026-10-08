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
