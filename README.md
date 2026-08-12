# chatgpt2api + any-auto-register 合并部署（胶水层）

把两个原版项目**原样并排运行**，仅通过一个独立的「胶水层（glue）」把 any-auto-register 注册成功的账号自动转发入库 chatgpt2api 账号池，实现「一个项目部署」即可完成 **ChatGPT 账号注册 → 取号 → 反代成 API key** 的完整链路。

两个原版项目代码**均不修改**，所有适配都在 glue 层。

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

## 快速开始

### 1. 准备（submodule 拉取两个原版）

```bash
git clone --recurse-submodules <本仓库地址>
cd <本仓库>
# 若已 clone 忘记 --recurse-submodules：
git submodule update --init --recursive
```

### 2. 配置代理（必须）

注册需要能访问 ChatGPT 的出口 IP（数据中心 IP 会被 OpenAI 风控）。二选一：

**A. mihomo 聚合订阅（推荐，自动轮换节点）**
在 `.env` 或 shell 中导出：
```bash
export MOHOMO_SUB_URL="https://你的clash订阅链接?clash=3&extend=1"
```
（也可直接把 `mihomo/config.yaml` 里的 `SUBSCRIPTION_URL_PLACEHOLDER` 替换成订阅链接）

**B. anytls 单节点（备用）**
```bash
export ANYTLS_SERVER="host:port"
export ANYTLS_PASSWORD="password"
export ANYTLS_SNI="host"
```

### 3. 配置邮箱域（必须）

在 any-auto-register 控制台（:8000 → Settings）配置 CFWorker 邮箱服务，并**使用干净的、非共享标记的邮箱域**（共享免费域如 `196856.xyz` 易被 OpenAI 整域封禁，导致注册即死）。

### 4. 启动

```bash
docker compose up -d --build
```

访问：
- ChatGPT 反代：http://localhost:3080
- 注册端控制台：http://localhost:8000
- 胶水层管理 API：http://localhost:8080 （`/healthz`、`/auto-register/status`、`/sync-now`）

## 胶水层（glue）配置项

通过环境变量注入（docker-compose.yml 的 glue.environment）：

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
| `CHATGPT2API_AUTH_KEY` | test_key_123 | c2a 管理 key |
| `GLUE_TOKEN` | glue-shared-secret | glue 自身鉴权 token |

## 数据流

1. any-auto-register 注册成功（走 mihomo/anytls 代理绕过 IP 风控）
2. glue 每 10s 轮询，把 aar `status=registered` 的账号转推 c2a `POST /api/accounts`
3. c2a 账号池立即可被 `/v1` 反代消费
4. 自动补货：c2a 可用账号 < `MIN_AVAILABLE` 且近期有流量 → glue 调 aar 注册补充
5. 反向清理：aar 标记 `invalid` 的账号，下次同步从 c2a 删除

## 已知限制

- **AT 模式账号短命**：本部署默认用 access_token 模式注册（避开 RT 模式的手机验证），access_token 短期有效、过期后无法自动刷新。靠「自动补货」对冲——旧 token 失效，新账号自动顶上。
- **邮箱域封禁**：若使用被 OpenAI 标记的共享邮箱域，账号会批量作废。请使用干净邮箱域。
- **aar 对 chatgpt 平台不主动标 invalid**（原版 `check_accounts_valid` 跳过 chatgpt），故失效 chatgpt 账号的清理依赖注册/使用时的检测 + 自动补货循环。

## 目录结构

```
.
├── docker-compose.yml
├── glue/
│   ├── glue.py            # 胶水层主程序
│   ├── Dockerfile
│   └── requirements.txt
├── mihomo/
│   └── config.yaml        # clash-meta 配置（订阅占位，运行时注入）
├── chatgpt2api/           # submodule: 原版消费端（不修改）
└── any-auto-register/     # submodule: 原版生产端（不修改）
```
