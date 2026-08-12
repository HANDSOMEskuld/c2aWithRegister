# chatgpt2api + any-auto-register 合并部署（胶水层）

把两个原版项目**原样并排运行**，仅通过一个独立的「胶水层（glue）」把 any-auto-register 注册成功的账号自动转发入库 chatgpt2api 账号池，实现「一个项目部署」即可完成 **ChatGPT 账号注册 → 取号 → 反代成 API key** 的完整链路。

两个原版项目代码**均不修改**，所有适配都在 glue 层。

---

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Docker Compose (单项目编排)                                  │
│                                                               │
│  chatgpt2api (原版 :80)  ← 3080 原生直连                      │
│       ↑ 账号池 (POST /api/accounts)                           │
│       │                                                       │
│  glue (胶水层 :8080)                                          │
│    ├─ 主动同步: 每 10s 扫 aar registered 账号 → 推 c2a        │
│    ├─ 反向清理: aar invalid 账号 → 从 c2a 删除               │
│    ├─ 自动补货: 可用 < MIN_AVAILABLE 且近期有流量 → 调 aar 注册 │
│    └─ 流量感知: 轮询 c2a /api/logs 判断 m 分钟内有调用        │
│       ↑ 注册 API                                              │
│  any-auto-register (原版 :8000)                               │
│       └─ 注册时从代理池取代理                                  │
│            ↑                                                   │
│  mihomo (clash-meta sidecar)  ← 聚合你的订阅全部节点, 1081    │
│  anytls-sidecar (可选备用单节点代理)  ← 1080                  │
└─────────────────────────────────────────────────────────────┘
```

服务：
- `chatgpt2api` —— 消费端（ChatGPT 反代），端口 3080
- `any-auto-register` —— 生产端（账号注册），端口 8000
- `glue` —— 胶水层（同步 + 自动补货 + 流量感知），端口 8080
- `mihomo` —— clash-meta sidecar，加载你的 Clash 订阅，聚合全部节点做负载均衡，aar 内 `127.0.0.1:1081` 即代理
- `anytls-sidecar` —— 可选备用单节点代理，aar 内 `127.0.0.1:1080`

> mihomo / anytls 以 `network_mode: service:any-auto-register` 与 aar 共享网络栈，所以 aar 容器内访问 `127.0.0.1:1081` / `127.0.0.1:1080` 即对应代理。

---

## 一、部署

### 1. 获取代码（含两个原版 submodule）

```bash
git clone --recurse-submodules <本仓库地址>
cd <本仓库>
# 若已 clone 但忘记 --recurse-submodules：
git submodule update --init --recursive
```

### 2. 配置代理（必须）

OpenAI 对数据中心 IP 有注册风控，**必须用住宅/家宽代理**才能稳定注册。本项目提供两种代理接入方式，aar 注册时从代理池取用。

**方式 A：mihomo 聚合 Clash 订阅（推荐，自动轮换节点）**

编辑 `docker-compose.yml` 的 `mihomo.environment`，填入你的 Clash 订阅链接：
```yaml
  mihomo:
    environment:
      MOHOMO_SUB_URL: "https://你的clash订阅链接?clash=3&extend=1"
```
启动后 mihomo 会拉取订阅、聚合全部节点做负载均衡，aar 代理池指向 `socks5://127.0.0.1:1081` 即可用上所有节点。

> 也可直接把 `mihomo/config.yaml` 里的 `SUBSCRIPTION_URL_PLACEHOLDER` 替换成订阅链接（不使用环境变量时）。

**方式 B：anytls 单节点（备用）**

编辑 `docker-compose.yml` 的 `anytls-sidecar.environment`：
```yaml
  anytls-sidecar:
    environment:
      ANYTLS_SERVER: "host:port"
      ANYTLS_PASSWORD: "password"
      ANYTLS_SNI: "host"
```
aar 代理池指向 `socks5://127.0.0.1:1080`。

**把代理加入 aar 代理池（关键一步）：**
无论用哪种代理，都要让 aar「知道」这个代理。aar 启动后，通过 aar 的代理管理 API 添加（容器网络内 `127.0.0.1:1081` 即 mihomo）：

```bash
# mihomo 方式
curl -X POST http://localhost:8000/api/proxies \
  -H "Content-Type: application/json" \
  -d '{"url":"socks5://127.0.0.1:1081","region":"sub"}'

# anytls 方式
curl -X POST http://localhost:8000/api/proxies \
  -H "Content-Type: application/json" \
  -d '{"url":"socks5://127.0.0.1:1080","region":"anytls"}'
```

> 代理添加一次即持久化在 aar 数据库，重启 aar 仍在。验证：`curl http://localhost:8000/api/proxies`。

### 3. 配置邮箱域（必须，且要用干净域名）

注册需要能收发验证码的邮箱。本项目用 aar 内置的 CFWorker 邮箱服务。

在 **any-auto-register 控制台（:8000 → Settings）** 配置：
- `mail_provider` = `cfworker`
- `cfworker_api_url` = 你的 CFWorker 邮箱服务地址
- `cfworker_admin_token` = 你的 token
- `cfworker_domain` = **用一个干净的、非共享标记的邮箱域**（不要用 `196856.xyz` 这类被 OpenAI 整域封禁的共享免费域，否则账号注册即死）

### 4. 启动

```bash
docker compose up -d --build
```

查看状态：
```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
```

各服务应均为 `Up`。

---

## 二、部署后如何使用

### 访问入口

| 地址 | 用途 |
|---|---|
| `http://localhost:3080` | **ChatGPT 反代**（chatgpt2api），直接当 OpenAI API 用 |
| `http://localhost:8000` | **注册端控制台**（any-auto-register），手动注册/查看账号/配置 |
| `http://localhost:8080` | **胶水层管理 API**（glue） |

### 用 ChatGPT 反代

把 `http://localhost:3080` 当作 OpenAI 的 base_url 使用，API key 用 chatgpt2api 的控制台里生成的 key：

```bash
curl http://localhost:3080/v1/chat/completions \
  -H "Authorization: Bearer <你的c2a的key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
```

账号池里的 ChatGPT 账号会被自动取出反代。

### 手动注册账号

方式一：aar 控制台（:8000）页面点「注册」，填写平台 chatgpt、数量等即可。
方式二：调 aar 注册 API：
```bash
curl -X POST http://localhost:8000/api/tasks/register \
  -H "Content-Type: application/json" \
  -d '{"platform":"chatgpt","email":null,"password":null,"count":1,
       "concurrency":1,"register_delay_seconds":0,"proxy":null,
       "executor_type":"protocol","captcha_solver":"local_solver",
       "extra":{"chatgpt_registration_mode":"access_token"}}'
```

> **注册模式建议用 `access_token`**：本部署默认 glue 自动注册也用此模式。原因——`refresh_token`(RT) 模式注册会走到 OpenAI 手机验证（add_phone），aar 未配 SMStoMe 时会卡住失败；而 `access_token`(AT) 模式可顺利注册成功进账号池（代价是 token 短期有效，靠自动补货对冲）。

### 账号自动同步 & 自动补货

配置好后**无需手动干预**：
- 注册的账号会在 ~10 秒内自动进 chatgpt2api 账号池（glue 轮询同步）
- 当可用账号 < `MIN_AVAILABLE`(默认5) 且最近 `TRAFFIC_WINDOW_MIN`(默认15)分钟内有反代调用时，glue 自动调 aar 注册补充
- aar 标记失效的账号，会在下次同步时从 chatgpt2api 删除，保持账号池干净

### 胶水层管理接口

```bash
# 健康检查
curl http://localhost:8080/healthz

# 查看自动注册状态（可用账号数、配置、上次注册结果）
curl -H "Authorization: Bearer test_key_123" http://localhost:8080/auto-register/status

# 手动触发一次同步（把 aar 已注册账号推到 c2a）
curl -X POST -H "Authorization: Bearer test_key_123" http://localhost:8080/sync-now

# 手动触发一次自动注册检查
curl -X POST -H "Authorization: Bearer test_key_123" http://localhost:8080/auto-register/trigger
```

---

## 三、配置项详解

### glue 环境变量（docker-compose.yml 的 `glue.environment`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SYNC_INTERVAL` | 10 | 主动同步轮询间隔（秒） |
| `AUTO_REGISTER_ENABLED` | true | 自动补货开关 |
| `MIN_AVAILABLE` | 5 | 可用账号低于此数触发补注册 |
| `REGISTER_BATCH` | 2 | 每次补注册几个 |
| `ONLY_ON_TRAFFIC` | true | 仅最近有流量时才补（省代理流量） |
| `TRAFFIC_WINDOW_MIN` | 15 | 流量窗口（分钟） |
| `CHECK_INTERVAL` | 60 | 自动补货检查间隔（秒） |
| `AAR_REGISTER_MODE` | 空 | 空=用 aar 全局配置；可填 `access_token` / `refresh_token` |
| `C2A_BASE_URL` | http://chatgpt2api:80 | c2a 内部地址 |
| `CHATGPT2API_AUTH_KEY` | test_key_123 | c2a 管理 key（也是 c2a API 的 Bearer） |
| `GLUE_TOKEN` | glue-shared-secret | glue 自身管理接口鉴权 token |

修改后重建生效：`docker compose up -d glue`

### 代理相关（docker-compose.yml）

| 变量 | 说明 |
|---|---|
| `MOHOMO_SUB_URL` | mihomo 的 Clash 订阅链接（方式 A） |
| `ANYTLS_SERVER` / `ANYTLS_PASSWORD` / `ANYTLS_SNI` | anytls 单节点参数（方式 B） |

### aar 控制台关键配置（:8000 → Settings）

- 邮箱：`mail_provider=cfworker` + CFWorker 服务地址/token + **干净邮箱域**
- `contribution_mode=custom` 已由 compose 注入（aar 注册成功会尝试推 glue，但 AT 模式因无 refresh_token 不触发，故由 glue 主动轮询兜底）

---

## 四、数据流

1. any-auto-register 注册成功（走 mihomo/anytls 代理绕过 IP 风控）
2. glue 每 10s 轮询，把 aar `status=registered` 的账号转推 c2a `POST /api/accounts`
3. c2a 账号池立即可被 `/v1` 反代消费
4. 自动补货：c2a 可用账号 < `MIN_AVAILABLE` 且近期有流量 → glue 调 aar 注册补充
5. 反向清理：aar 标记 `invalid` 的账号，下次同步从 c2a 删除

---

## 五、已知限制

- **AT 模式账号短命**：本部署默认用 access_token 模式注册（避开 RT 模式的手机验证），access_token 短期有效、过期后无法自动刷新。靠「自动补货」对冲——旧 token 失效，新账号自动顶上。
- **邮箱域封禁**：若使用被 OpenAI 标记的共享邮箱域，账号会批量作废。请使用干净邮箱域。
- **aar 对 chatgpt 平台不主动标 invalid**（原版 `check_accounts_valid` 跳过 chatgpt），故失效 chatgpt 账号的清理依赖注册/使用时的检测 + 自动补货循环。

---

## 六、目录结构

```
.
├── docker-compose.yml
├── glue/
│   ├── glue.py            # 胶水层主程序
│   ├── Dockerfile
│   └── requirements.txt
├── mihomo/
│   └── config.yaml        # clash-meta 配置（订阅占位，运行时注入）
├── README.md
├── chatgpt2api/           # submodule: 原版消费端（不修改）
└── any-auto-register/     # submodule: 原版生产端（不修改）
```
