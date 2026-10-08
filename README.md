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
在你的电脑上（WorkBuddy 客户端已登录运行）执行：

```bash
# 自动注入 WB_REFRESH_TOKEN（优先 gh CLI；其次 GH_TOKEN 走 API）
python3 scripts/refresh_token_local.py --repo <你的owner>/<仓库名> \
    --feishu-webhook "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" \
    --feishu-secret "你的加签密钥（可选）"
```

- 脚本复用已安装的 `totorosir-workbuddy-checkin` 技能导出长效令牌，自动处理 5.6.2+ 加密登录态。
- 明文令牌只经 `gh secret set -b` / HTTPS 直传，**不写文件、不进日志**；终端只打印脱敏串。
- 没装 `gh` 也没 `GH_TOKEN` 时，脚本会给出手动命令，你复制令牌到 GitHub 仓库
  Settings → Secrets → Actions 里新建 `WB_REFRESH_TOKEN` / `WB_FEISHU_WEBHOOK` / `WB_FEISHU_SECRET` 即可。

### 3. 推送本仓库到 GitHub
```bash
git init && git add -A && git commit -m "add workbuddy checkin actions"
git remote add origin git@github.com:<owner>/<仓库名>.git
git push -u origin main
```

### 4.（可选）云端自续期 —— 实现「本机永久关机也能跑」
长效令牌本身只有 ~30 天有效期。默认模式下需每 ~30 天开一次本机重注入；
启用自续期后，云端每次刷新会把新 refreshToken 写回 Secret，从此本机只需最初注入一次。

做法：新建一个**细粒度 PAT**（Fine-grained PAT），仅授予本仓库的
「Actions › Read and write」权限（不要给整账号 `repo` 全权），把它作为
`WB_GH_PAT` Secret 加到仓库。Workflow 检测到该 Secret 后自动安装 PyNaCl 并写回。

```bash
# 在 GitHub 仓库 Settings → Secrets → Actions 新建：
#   WB_GH_PAT = 上面拿到的细粒度 PAT
```
> 安全说明：`WB_GH_PAT` 具备写本仓库 Actions Secret 的能力，务必用「仅限本仓库」的
> 细粒度令牌，不要用有 `repo` 全权的经典 PAT。它也只存在于 GitHub Secret，不进本仓库/日志。

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
