# board-data — 看板数据快照仓库

RLCD 桌面看板（ESP32-S3）的 Neko 数据源（GitHub Actions 定时快照版）。

## 架构

```
[GitHub Actions cron 每10分钟, 北京 7:00-22:50]  (免费, GITHUB_TOKEN 提交)
  └─ sync.py: 拉东财7x24新闻 / 腾讯分时(多股票) / OWM 天气 / DeepSeek 余额 / 国际指数
       → 清洗 → 生成 data.json + tick.txt + tick_<code>.txt + state.json + tick_history.json
  └─ commit+push → purge.jsdelivr.net (jsDelivr 分支缓存 ~24h, 必须刷)
[板子任意网络]
  data.json: cdn.jsdelivr.net/gh/P617p/board-data@main/data.json (主)
             raw.githubusercontent.com/... (副) + ghproxy.net (末位兜底)
  tick.txt / tick_<code>.txt:  同上三路
[家里 AstrBot] neko_board 插件用 PAT 写 reminders.json → 下轮快照合并 (R2: 插件已停用, 保留)
```

- **数据延迟**：≤10 分钟（与板子刷新节奏一致）
- **API key**：仅存 Actions secrets（DEEPSEEK_API_KEY / WEATHER_API_KEY），绝不进仓库
- **余额金额公开**（仓库为 public，主人已接受）

## secrets 配置（仅两个）

```bash
gh secret set DEEPSEEK_API_KEY -R P617p/board-data
gh secret set WEATHER_API_KEY -R P617p/board-data
```

天气坐标非敏感，已写死在 workflow（WEATHER_LAT/LON 崇明城桥镇）。

## 生成物

| 文件 | 说明 |
|------|------|
| `data.json` | 板子 /neko_data 同构（deepseek/weather/calendar/greeting/message/reminders/news/**indices**），≤8KB；indices = R6 国际指数（黄金/纳指/日经/KOSPI） |
| `tick.txt` | 分时 3 日文本协议 `D|YYYYMMDD|prev_close|HHMM price vol;...`（主股票 sz003041） |
| `tick_<code>.txt` | R5 附加股票分时（同协议，如 tick_sz000001.txt；列表见 sync.py `EXTRA_TICK_CODES`） |
| `state.json` | 跨天花费基准 + 喝水提醒冷却（跨轮持久） |
| `tick_history.json` | 分时 3 日窗口（跨轮持久，最多 5 自然日，**R5 起按代码分键**） |
| `reminders.json` | ⚠️ AstrBot 插件写入（PAT），sync.py 只读不写，勿手工覆盖（R2 插件已停用，字段保留） |

> R3（2026-08-13）：新闻源由新浪滚动改为**东财 7x24 证券快讯**（fastColumn=101，需 Referer），新浪证券频道 lid=2517 兜底；全部输出经 GB2312 清洗 ≤40 字。
> R6（2026-08-13）：指数走**东财 push2delay**（push2 主域名易限流，必须用延迟节点；f2/f3 ×100 服务端已 ÷100）+ 新浪 hf_XAU（黄金，GBK）。

## 运维

- 手动补跑：仓库 Actions 页 → Run workflow（workflow_dispatch）
- purge 失败：jsDelivr 旧数据 ≤24h，raw 兜底 URL 始终新鲜；连续失败可把 config.h URL1/URL2 顺序对调
- 提醒时限：最后一次快照 22:50（北京），**当天提醒请安排在 22:40 前**（22:50 后创建次日才上板）
- 本地验证：`python3 sync.py`（同目录 .env 提供 key；Windows 缺 tzdata 时 `pip install tzdata`）

## 迁移记录

2026-08-04：由小主机常驻 `tools/neko_local/run_server.py`（192.168.71.82:8000）迁移而来，小主机服务已停用，neko_local 保留归档。
