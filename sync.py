#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Neko 看板数据同步脚本 (GitHub Actions 定时快照版)
==================================================
替代小主机常驻 run_server.py: 每次运行采集一次全量数据, 生成:
  data.json       板子 /neko_data 同构 (deepseek/weather/calendar/greeting/message/reminders/news)
  tick.txt        分时 3 日文本协议 (板子 /tick_history 同构)
  state.json      跨天花费基准 + 喝水提醒冷却 (随仓库提交, 跨轮持久)
  tick_history.json 分时 3 日窗口 (随仓库提交)

运行环境: GitHub Actions (ubuntu-latest, cron 每 10 分钟, 北京 7:00-22:50)
  secrets 经环境变量注入: DEEPSEEK_API_KEY / WEATHER_API_KEY
本地验证: 同目录 .env (KEY=VALUE, 已 gitignore) 或直接设置环境变量

⚠️ 所有"当前时间"判断必须 now_cn() (Actions 环境时区是 UTC)
"""
import base64
import json
import os
import random
import re
import ssl
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, 'state.json')
TICK_HISTORY_FILE = os.path.join(BASE_DIR, 'tick_history.json')
REMINDERS_FILE = os.path.join(BASE_DIR, 'reminders.json')
DATA_FILE = os.path.join(BASE_DIR, 'data.json')

# 插件 (AstrBot neko_board) 用 PAT 写入的提醒文件, raw 拉最新 (checkout 副本是 job 开始时快照)
REMINDERS_RAW_URL = 'https://raw.githubusercontent.com/P617p/board-data/main/reminders.json'

# 加载同目录 .env (仅本地验证用; Actions 里 secrets 已注入, setdefault 不覆盖)
ENV_FILE = os.path.join(BASE_DIR, '.env')
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

WEATHER_KEY = os.environ.get('WEATHER_API_KEY', '')
WEATHER_CITY = os.environ.get('WEATHER_CITY', 'Shanghai')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')

# ============================================================
# 时段消息表 (分钟 = hour*60 + minute) — 与旧服务端完全一致
# ============================================================
SCHEDULES = [
    (0,          8 * 60,      'Zzz 睡眠时间，主人晚安～'),
    (8 * 60 + 30, 8 * 60 + 50, '主人早！该称体重打卡啦！'),
    (8 * 60 + 50, 9 * 60 + 10, '检查一下今天的工作计划进度吧！'),
    (17 * 60,    17 * 60 + 30, '收工啦！去锻炼放松一下吧～'),
    (22 * 60,    24 * 60,      '睡觉时间到！放下手机好好休息'),
]

# 工作时间段 (周一~五): 随机提醒喝水/走动
WORK_HOURS = ((9 * 60 + 10, 12 * 60), (13 * 60, 17 * 60))
WORK_REMINDERS = ['喝口水休息一下吧！', '站起来走两步，活动活动！',
                  '记得喝水哦，别坐太久～', '起来伸个懒腰，放松一下！']
WORK_REMIND_COOLDOWN_MIN = 30   # 喝水提醒冷却 (分钟)

# 看板计划提醒窗口: 触发后 30 分钟内板子优先显示 (配合板子端 RTC 准点匹配)
BOARD_WINDOW_SEC = 30 * 60
BOARD_MAX_ITEMS = 20            # reminders.json 环形条数

# 问候语池 (纯文字, 无 emoji, 全部 GB2312 内字符 — 板子字体只覆盖 GB2312;
# 前缀 "Neko：" 由板子端添加)
STOCK_GREETINGS = [
    '主人，该看看股票了喵~',
    '开盘啦开盘啦！今天会涨吗？',
    '盯盘要紧，但别忘了喝水哦主人！',
    '主人专心工作，我帮你盯着盘！',
]

# 按时间段分桶, 桶内随机 — 避免"傍晚说早上好"式时间语义错乱
# (分钟 = hour*60 + minute; 与 SCHEDULES 窗口互补, 覆盖 SCHEDULES 外的空档)
GREETING_BUCKETS = [
    # (起始分钟, 结束分钟, 文案列表)
    (8 * 60, 11 * 60, [
        '主人早上好！今天也要元气满满哦！',
        '早晨好呀，新的一天要开心哦～',
        '主人吃早饭了吗？记得按时吃饭！',
    ]),
    (11 * 60, 17 * 60, [
        '今天天气不错呢！',
        '加油搬砖！为了更好的明天！',
        '今天也要做个开心的打工人！',
        '主人有什么吩咐？',
        '工作再忙也要记得摸摸鱼哦~',
        '今天想吃什么好吃的？',
    ]),
    (17 * 60, 21 * 60, [
        '辛苦一天啦！好好休息喵～',
        '收工啦！主人今天也辛苦了！',
        '忙了一天，放松一下犒劳自己吧～',
    ]),
    (21 * 60, 24 * 60, [
        '晚安喵~ 祝主人好梦',
        '夜深啦，主人早点休息哦～',
    ]),
]

# 新闻: 板子 124×56px, 12px 字行高 14 → 4 行 ≈ 40 字 (含标点)
NEWS_MAX_CHARS = 40
NEWS_MIN_CHARS = 10
NEWS_BAD_SUFFIX = ('背后', '内幕', '揭秘', '之谜', '真相', '始末', '风云', '大戏', '玄机')
NEWS_URL = 'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=50'

# 分时: 与板子 config.h STOCK_CODE 必须一致
TICK_STOCK_CODE = 'sz003041'
TICK_TENCENT_URL = ('https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=' + TICK_STOCK_CODE)
TICK_PARTIAL_STALE_SEC = 600    # 盘中 partial 超 10 分钟陈旧则补拉 (匹配 Actions 节奏)
TICK_MAX_ENTRIES = 5            # 缓存最多 5 个自然日 (≥3 个交易日)

# ============================================================
# K线策略提醒 (2026-08-05 新增)
#   信号: 金叉/死叉(MA5×MA20, 放量确认) / MACD金叉死叉 / 布林突破 /
#         放量异动 / 日线大波动 / 主力净流入
#   时机: 放量/大波动/资金流盘中实时, 均线类收盘后确认 (防盘中假信号)
#   防重复: state.json strat {date, fired, count, key, ts, text}
#           同一信号同日只提醒一次, 单日上限 STRAT_MAX_PER_DAY 条
# ============================================================
STRATEGY_KLINE_URL = ('https://money.finance.sina.com.cn/quotes_service/api/'
                      'json_v2.php/CN_MarketData.getKLineData')
STRATEGY_KLINE_DAYS = 120        # MACD EMA26 预热需 60+ 根
STRATEGY_KLINE_SYMBOL = TICK_STOCK_CODE   # 与分时同股票


def eastmoney_secid():
    c = TICK_STOCK_CODE.lower()
    return ('1' if c.startswith('sh') else '0') + '.' + c[2:]


MONEYFLOW_URL = ('http://push2.eastmoney.com/api/qt/stock/fflow/kline/get?'
                 'lmt=1&klt=101&secid=%s&fields1=f1,f2,f3,f7'
                 '&fields2=f51,f52,f53,f54,f55,f56' % eastmoney_secid())

STRAT_BIG_CHG_PCT = 5.0          # 日线大波动阈值 %
STRAT_VOL_MULT = 1.8             # 放量: 预计全天量 / 5日均量
STRAT_GOLD_VOL_MULT = 1.3        # 金叉组合确认: 放量倍数 (降震荡市假信号)
STRAT_MONEYFLOW_YUAN = 20000000  # 主力净流入阈值 元 (2000万)
STRAT_ALERT_MINUTES = 60         # 提醒窗口: 触发后 message 优先显示分钟
STRAT_MAX_PER_DAY = 5            # 单日提醒上限

ctx = ssl.create_default_context()


def now_cn():
    return datetime.now(TZ)


def http_get(url, headers=None, timeout=10):
    req = Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read().decode('utf-8')
    except Exception:
        return None


def atomic_write(path, text):
    """原子替换写入 (防止半写文件被板子读到)"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


# ============================================================
# 新闻清洗链 (GB2312 安全 — 板子字体只覆盖 GB2312 全量 7155 字)
# ============================================================

def strip_emoji(s):
    return ''.join(c for c in s
                   if not (0x1F000 <= ord(c) <= 0x1FAFF)
                   and not (0x2600 <= ord(c) <= 0x27BF))


def gb2312_safe(s):
    out = []
    for c in s:
        try:
            c.encode('gb2312')
            out.append(c)
        except Exception:
            out.append(' ')
    return ''.join(out)


def smart_truncate(s, max_len):
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    for i in range(len(cut) - 1, max_len - 8, -1):
        if cut[i] in '。！？，；、：':
            return cut[:i] + '…'
    return cut[:max_len - 1] + '…'


def clean_news_title(s, max_len=NEWS_MAX_CHARS):
    s = strip_emoji(s or '')
    s = re.sub(r'[\[\]【】()（）《》<>{}|#*・]', '', s)
    s = gb2312_safe(s)
    s = ' '.join(s.split())
    return smart_truncate(s, max_len)


# ============================================================
# 状态 (跨天花费基准 / 喝水冷却) — 随仓库提交跨轮持久
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        atomic_write(STATE_FILE, json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def load_old_data_json():
    """上次快照的 data.json (checkout 里有), 各数据源失败时兜底旧值"""
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ============================================================
# DeepSeek 余额 + 当日花费 (主人方案: 昨天余额 - 当前余额)
# ⚠️ 跨天 rollover 只在 API 成功时推进 (失败不得推进 date/day_start_balance,
#    否则次日花费基准错乱 — 与旧服务端语义一致)
# ============================================================

def fetch_deepseek(old):
    if not DEEPSEEK_KEY:
        return {'dailyTokens': None, 'cacheHitRate': None, 'requestCount': None,
                'cost': None, 'balance': None}

    bal = None
    try:
        resp = http_get('https://api.deepseek.com/user/balance',
                        {'Authorization': 'Bearer ' + DEEPSEEK_KEY}, 5)
        data = json.loads(resp or '{}')
        infos = data.get('balance_infos') or []
        new_bal = sum(float(i.get('total_balance', 0)) for i in infos) if infos else None
        if new_bal is not None:
            bal = new_bal
    except Exception:
        pass

    st = load_state()
    if bal is None:
        # 拉取失败: 兜底旧快照值, 不推进跨天基准
        if old and old.get('balance'):
            return {'dailyTokens': None, 'cacheHitRate': None, 'requestCount': None,
                    'cost': old.get('cost', 0), 'balance': old['balance']}
        return {'dailyTokens': None, 'cacheHitRate': None, 'requestCount': None,
                'cost': None, 'balance': None}

    today = now_cn().strftime('%Y-%m-%d')
    if st.get('date') != today:
        # 跨天/首次: 昨天的最后余额成为今日基准
        base = st.get('last_balance')
        st['day_start_balance'] = base if base is not None else bal
        st['date'] = today
    cost = max(0.0, round(st['day_start_balance'] - bal, 2))
    st['last_balance'] = bal
    save_state(st)

    return {'dailyTokens': None, 'cacheHitRate': None, 'requestCount': None,
            'cost': cost, 'balance': bal}


# ============================================================
# 天气 (OpenWeatherMap, 崇明城桥镇; 失败旧值兜底 → 模拟)
# ============================================================

def mock_weather():
    h = now_cn().hour
    t = round(random.uniform(20, 28), 1) if 8 <= h < 20 else round(random.uniform(16, 20), 1)
    return {'temperature': t, 'description': random.choice(['晴', '多云', '阴']),
            'warning': '', 'humidity': random.randint(45, 80)}


def fetch_weather(old):
    if not WEATHER_KEY:
        return mock_weather()
    try:
        if os.environ.get('WEATHER_LAT') and os.environ.get('WEATHER_LON'):
            url = ('https://api.openweathermap.org/data/2.5/weather?lat=%s&lon=%s&appid=%s&units=metric&lang=zh_cn'
                   % (os.environ['WEATHER_LAT'], os.environ['WEATHER_LON'], WEATHER_KEY))
        else:
            url = ('https://api.openweathermap.org/data/2.5/weather?q=%s&appid=%s&units=metric&lang=zh_cn'
                   % (WEATHER_CITY, WEATHER_KEY))
        data = json.loads(http_get(url, timeout=5) or '{}')
        if 'main' not in data:
            raise ValueError('bad weather resp')
        warn = ''
        if data['wind']['speed'] >= 17.2:
            warn = '大风预警：风速%.0fm/s' % data['wind']['speed']
        return {'temperature': round(data['main']['temp'], 1),
                'description': data['weather'][0]['description'],
                'warning': warn, 'humidity': data['main']['humidity']}
    except Exception:
        old_w = old.get('weather') or {}
        if old_w.get('temperature') is not None:
            return old_w
        return mock_weather()


# ============================================================
# 看板消息: 计划提醒 / 时段消息 / 喝水提醒 / 问候池
# ============================================================

def get_board_messages():
    """读提醒池: 优先 raw 拉最新 (插件刚 PUT 的提醒), 失败退 checkout 本地副本"""
    items = []
    resp = http_get(REMINDERS_RAW_URL, timeout=10)
    if resp:
        try:
            items = json.loads(resp).get('items', [])
        except Exception:
            items = []
    if not items:
        try:
            with open(REMINDERS_FILE, encoding='utf-8') as f:
                items = json.load(f).get('items', [])
        except Exception:
            items = []
    return sorted(items, key=lambda x: x.get('ts', 0), reverse=True)


def same_local_day(ts, now):
    dt = datetime.fromtimestamp(ts, TZ)
    return (dt.year, dt.month, dt.day) == (now.year, now.month, now.day)


def today_reminders():
    """当天看板计划提醒清单 [{t:'HH:MM', text}] — 板子端 RTC 准点匹配用"""
    now = now_cn()
    out = []
    for it in get_board_messages():
        if len(out) >= 8:
            break
        if same_local_day(it.get('ts', 0), now):
            dt = datetime.fromtimestamp(it['ts'], TZ)
            out.append({'t': '%02d:%02d' % (dt.hour, dt.minute), 'text': it['text']})
    return out


def fetch_kline():
    """新浪日线 (datalen=120, 供 MACD/BOLL 预热); 失败返回 None"""
    url = (STRATEGY_KLINE_URL + '?symbol=%s&scale=240&ma=no&datalen=%d'
           % (STRATEGY_KLINE_SYMBOL, STRATEGY_KLINE_DAYS))
    resp = http_get(url, timeout=8)
    if not resp:
        return None
    try:
        arr = json.loads(resp)
        out = []
        for d in arr:
            if not d.get('close'):
                continue
            out.append({'date': str(d.get('day', ''))[:10], 'open': float(d.get('open', 0)),
                        'high': float(d.get('high', 0)), 'low': float(d.get('low', 0)),
                        'close': float(d.get('close', 0)), 'volume': float(d.get('volume', 0))})
        return out or None
    except Exception:
        return None


def fetch_moneyflow():
    """东方财富个股资金流 (klt=101 日线, 最新一根): 主力净流入(元)=大单+超大单; 失败 None"""
    try:
        resp = http_get(MONEYFLOW_URL, timeout=8)
        data = json.loads(resp or '{}')
        klines = (data.get('data') or {}).get('klines') or []
        if not klines:
            return None
        parts = klines[-1].split(',')
        if len(parts) < 6:
            return None
        return float(parts[1])   # f52 = 主力净流入 (元)
    except Exception:
        return None


def ema_series(values, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def traded_progress(t):
    """当前交易时刻进度 (0-1): 上午 0-120 分钟 / 午休 120 / 下午 121-241"""
    if t <= 9 * 60 + 30:
        return 0.0
    if t <= 11 * 60 + 30:
        return (t - (9 * 60 + 30)) / 242.0
    if t < 13 * 60:
        return 120 / 242.0
    return (120 + (t - 13 * 60)) / 242.0


def check_strategies(kl, mf, now):
    """按当前时刻检查当日信号 (盘后=完整K线均线类, 盘中=量/价/资金实时类)
    返回 [{key, text}] 已按优先级排序; 非交易日/盘前返回 []"""
    t = now.hour * 60 + now.minute
    if now.weekday() >= 5 or t < 9 * 60 + 30:
        return []
    n = len(kl)
    if n < 2:
        return []
    is_after_close = t >= 15 * 60 + 5
    last, prev = kl[-1], kl[-2]
    signals = []
    closes = [x['close'] for x in kl]
    vols = [x['volume'] for x in kl]
    vol5 = sum(vols[-6:-1]) / 5.0 if n >= 6 else (sum(vols) / n if vols else 0)

    # --- 收盘后确认 (需完整日K, 防盘中假信号) — 均线类重要信号优先 ---
    if is_after_close and n >= 21:
        ma5 = sum(closes[-5:]) / 5.0
        ma20 = sum(closes[-20:]) / 20.0
        ma5_prev = sum(closes[-6:-1]) / 5.0
        ma20_prev = sum(closes[-21:-1]) / 20.0
        # 金叉/死叉 (MA5×MA20; 金叉要求放量确认 — 组合降假信号)
        gold_vol = vols[-1] >= vol5 * STRAT_GOLD_VOL_MULT
        if ma5_prev <= ma20_prev and ma5 > ma20:
            if gold_vol:
                signals.append({'key': 'gold', 'text': '真爱美家 放量金叉！MA5上穿MA20'})
        elif ma5_prev >= ma20_prev and ma5 < ma20:
            signals.append({'key': 'death', 'text': '真爱美家 死叉！MA5下穿MA20'})
        # MACD 金叉/死叉 (12/26/9)
        dif = ema_series(closes, 12)
        dea = ema_series(dif, 9)
        if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
            signals.append({'key': 'macdg', 'text': '真爱美家 MACD金叉！多方动能增强'})
        elif dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
            signals.append({'key': 'macdd', 'text': '真爱美家 MACD死叉，注意回调'})
        # 布林带突破 (20日 ±2σ)
        mid = ma20
        std = (sum((c - mid) ** 2 for c in closes[-20:]) / 20.0) ** 0.5
        if last['close'] >= mid + 2 * std:
            signals.append({'key': 'bollu', 'text': '真爱美家 突破布林上轨！'})
        elif last['close'] <= mid - 2 * std:
            signals.append({'key': 'bolld', 'text': '真爱美家 跌破布林下轨，注意风险'})

    # --- 盘中实时 (混合时机: 价/量/资金即时) ---
    # 日线大波动 (当日涨跌幅)
    if prev['close'] > 0:
        chg = (last['close'] - prev['close']) / prev['close'] * 100
        if abs(chg) >= STRAT_BIG_CHG_PCT:
            signals.append({'key': 'bigchg',
                            'text': '真爱美家 今日%s %.1f%%' % ('大涨' if chg > 0 else '大跌', chg)})
    # 放量异动: 盘后直接用实际量比, 盘中按时间折算预计全天量 (早盘放量也能报)
    if vol5 > 0 and (is_after_close or traded_progress(t) > 0.1):
        if is_after_close:
            est_vol = last['volume']
        else:
            est_vol = last['volume'] / traded_progress(t)
        if est_vol >= vol5 * STRAT_VOL_MULT:
            signals.append({'key': 'vol',
                            'text': '真爱美家 放量异动！量比%.1f' % (est_vol / vol5)})
    # 主力资金流 (东财实时)
    if mf is not None and mf >= STRAT_MONEYFLOW_YUAN:
        signals.append({'key': 'money',
                        'text': '真爱美家 主力净流入 %+.0f万' % (mf / 10000.0)})
    return signals


def update_strategy(kline, mf):
    """当日信号去重 + 写 state.json strat; 返回当前待显示提醒 {text} 或 None
    盘前/非交易日不更新 (防把昨日信号记到今日, 误挡今日真信号)"""
    now = now_cn()
    t = now.hour * 60 + now.minute
    if not kline or now.weekday() >= 5 or t < 9 * 60 + 30:
        return None
    today = now.strftime('%Y-%m-%d')
    st = load_state()
    strat = st.get('strat') or {}
    if strat.get('date') != today:
        strat = {'date': today, 'fired': [], 'count': 0}
    # 单日上限: 已满不再收新信号
    if strat['count'] >= STRAT_MAX_PER_DAY:
        # 窗口内仍显示当前提醒
        if strat.get('text') and int(time.time()) - strat.get('ts', 0) <= STRAT_ALERT_MINUTES * 60:
            return {'text': strat['text']}
        return None
    fired = set(strat.get('fired', []))
    for sig in check_strategies(kline, mf, now):
        if sig['key'] in fired:
            continue
        fired.add(sig['key'])
        strat['fired'] = sorted(fired)
        strat['count'] = strat.get('count', 0) + 1
        strat['key'] = sig['key']
        strat['text'] = sig['text']
        strat['ts'] = int(time.time())
        st['strat'] = strat
        save_state(st)
        print('[strategy] %s -> %s' % (now.strftime('%F %T'), sig['text']))
        return {'text': sig['text']}
    st['strat'] = strat
    save_state(st)
    # 已全部触发: 窗口内继续显示最后一条
    if strat.get('text') and int(time.time()) - strat.get('ts', 0) <= STRAT_ALERT_MINUTES * 60:
        return {'text': strat['text']}
    return None


def pick_greeting():
    """按时段分桶随机问候 (股票问候仅交易时段混入; 超窗兜底夜间桶)"""
    now = now_cn()
    t = now.hour * 60 + now.minute
    pool = None
    for start, end, texts in GREETING_BUCKETS:
        if start <= t < end:
            pool = list(texts)
            break
    if pool is None:
        pool = list(GREETING_BUCKETS[-1][2])
    if now.weekday() < 5 and 9 * 60 + 15 <= t <= 15 * 60 + 5:
        pool += STOCK_GREETINGS
    return strip_emoji(random.choice(pool))


def pick_message():
    """当前应显示的消息: 看板提醒窗口 > 时段消息 > 工作时间随机 > 问候池"""
    now = now_cn()
    now_ts = int(time.time())

    # 1. 当天看板消息在提醒窗口内 (触发后 30 分钟) → 优先显示
    for it in get_board_messages():
        it_ts = it.get('ts', 0)
        if it_ts and same_local_day(it_ts, now) and 0 <= now_ts - it_ts <= BOARD_WINDOW_SEC:
            return it['text']

    # 1.5 策略提醒 (K线信号触发后 STRAT_ALERT_MINUTES 分钟内优先显示)
    strat = load_state().get('strat') or {}
    if (strat.get('date') == now.strftime('%Y-%m-%d') and strat.get('text')
            and now_ts - strat.get('ts', 0) <= STRAT_ALERT_MINUTES * 60):
        return strat['text']

    t = now.hour * 60 + now.minute

    # 2. 时段消息
    for start, end, text in SCHEDULES:
        if start <= t < end:
            return text

    # 3. 工作时间随机喝水/走动提醒 (30 分钟冷却, 防连续刷屏)
    if now.weekday() < 5 and any(s <= t < e for s, e in WORK_HOURS):
        st = load_state()
        last = st.get('work_remind_ts', 0)
        if now_ts - last >= WORK_REMIND_COOLDOWN_MIN * 60 and random.random() < 0.25:
            st['work_remind_ts'] = now_ts
            save_state(st)
            return random.choice(WORK_REMINDERS)

    # 4. 问候池兜底 (按时段分桶, 股票问候仅交易时段混入; 休市不提醒看盘)
    return pick_greeting()


# ============================================================
# 头条新闻 (新浪滚动, 清洗链 + 质量过滤)
# ============================================================

def fetch_news(old):
    try:
        data = json.loads(http_get(NEWS_URL, timeout=8) or '{}')
        items = (data.get('result') or {}).get('data') or []
        for it in items:
            t = clean_news_title(it.get('title') or '')
            if not t:
                continue
            if len(t) < NEWS_MIN_CHARS:
                continue
            if t.endswith(NEWS_BAD_SUFFIX):
                continue
            return t
        return old.get('news') or ''
    except Exception:
        return old.get('news') or ''


# ============================================================
# 分时历史缓存 (最近 3 个交易日) — 腾讯 minute 接口每次只返回
# "当前时刻最近交易日": 盘中=今天 partial, 收盘后=最近交易日全天
# 数据日期必须取响应 qt[30] (周末/节假日腾讯回上一交易日, 勿用本机日期)
# ============================================================

def load_tick_history():
    try:
        with open(TICK_HISTORY_FILE, encoding='utf-8') as f:
            return json.load(f).get('entries', [])
    except Exception:
        return []


def save_tick_history(entries):
    try:
        atomic_write(TICK_HISTORY_FILE, json.dumps({'entries': entries}, ensure_ascii=False))
    except Exception:
        pass


def fetch_tencent_day():
    """拉腾讯分时 (当前时刻最近交易日), 过滤非交易时段点, 失败返回 None"""
    try:
        resp = http_get(TICK_TENCENT_URL)
        if not resp:
            return None
        data = json.loads(resp)
        code_data = (data.get('data') or {}).get(TICK_STOCK_CODE) or {}
        pts = ((code_data.get('data') or {}).get('data')) or []
        points = []
        for s in pts:
            try:
                parts = s.split()
                hhmm = int(parts[0])
                price = float(parts[1])
                vol = int(parts[2]) if len(parts) > 2 else 0
            except Exception:
                continue
            # 与板子 in_session_time 同规则: 9:30-11:30 / 13:00-15:00, 剔 15:00-15:30 尾巴
            if (930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500):
                points.append([hhmm, price, vol])
        if not points:
            return None
        qt = (code_data.get('qt') or {}).get(TICK_STOCK_CODE) or []
        try:
            prev_close = float(qt[3]) if len(qt) > 3 and qt[3] else None
        except Exception:
            prev_close = None
        if not prev_close or prev_close <= 0:
            prev_close = points[0][1]
        try:
            date_str = str(qt[30]).replace('-', '') if len(qt) > 30 and qt[30] else ''
        except Exception:
            date_str = ''
        if len(date_str) != 8 or not date_str.isdigit():
            date_str = now_cn().strftime('%Y%m%d')  # 兜底
        return {'date': date_str, 'complete': False,
                'prev_close': prev_close, 'points': points}
    except Exception:
        return None


def upsert_tick_entry(entries, entry):
    """同日期条目合并 (节假日腾讯返回上一交易日数据, 日期相同则更新而非追加);
    保留更完整的 (complete 优先, 其次点数多)"""
    for i, e in enumerate(entries):
        if e['date'] == entry['date']:
            if entry['complete']:
                entries[i] = entry
            elif not e['complete'] and len(entry['points']) >= len(e['points']):
                entries[i] = entry
            return
    entries.append(entry)
    entries.sort(key=lambda e: e['date'])


def tick_refresh_once():
    """单轮分时更新决策 (原 daemon 死循环摊平成一次运行; cron 已限定北京 7:00-22:50)"""
    entries = load_tick_history()
    now = now_cn()
    t = now.hour * 60 + now.minute
    is_weekday = now.weekday() < 5

    # 首次/缓存被清: 回填一次 (完整度由时刻决定)
    if not entries:
        d = fetch_tencent_day()
        if d:
            d['complete'] = (t >= 15 * 60 + 5)
            save_tick_history([d])
        return

    # 周末或时段外: 不调腾讯 (tick.txt 沿用缓存)
    if not is_weekday or t >= 23 * 60 or t < 7 * 60:
        return

    last = entries[-1]
    if t < 9 * 60 + 30 or t >= 15 * 60 + 5:
        # 盘前/收盘后: 最近条目非完整 → 补拉全天 (腾讯返回最近交易日完整数据)
        if not last.get('complete'):
            d = fetch_tencent_day()
            if d:
                d['complete'] = True
                upsert_tick_entry(entries, d)
                save_tick_history(entries[-TICK_MAX_ENTRIES:])
    else:
        # 盘中: 今天条目缺失或陈旧 (10 分钟) → 补拉 partial (板子盘中重启兜底)
        cur = last if last['date'] == now.strftime('%Y%m%d') else None
        if cur is None or (not cur['complete'] and
                           time.time() - cur.get('fetched_at', 0) > TICK_PARTIAL_STALE_SEC):
            d = fetch_tencent_day()
            if d:
                d['fetched_at'] = time.time()
                upsert_tick_entry(entries, d)
                save_tick_history(entries[-TICK_MAX_ENTRIES:])


def build_tick_response():
    """文本协议: 每行一天 D|YYYYMMDD|prev_close|HHMM price vol;... (升序, 旧在前)
    周一/新交易日修正: 交易日盘中且缓存无今天条目 → 只回最近 2 天
    (板子盘中本地采样今天后自然凑满 3 天, 避免显示 4 段)"""
    entries = load_tick_history()
    if not entries:
        return ''
    now = now_cn()
    t = now.hour * 60 + now.minute
    today = now.strftime('%Y%m%d')
    is_session = (9 * 60 + 30) <= t < (15 * 60)
    have_today = any(e['date'] == today for e in entries)
    take = 2 if (now.weekday() < 5 and is_session and not have_today) else 3
    lines = []
    for e in entries[-take:]:
        # %04d: HHMM 补前导零 (930 → "0930"), 板子 %2d%2d 拆 hh/mm 才正确
        pts = ';'.join('%04d %.2f %d' % (h, p, v) for h, p, v in e['points'])
        lines.append('D|%s|%.2f|%s' % (e['date'], e['prev_close'], pts))
    return '\n'.join(lines) + '\n'


# ============================================================
# 主流程
# ============================================================

def main():
    old = load_old_data_json()
    now = now_cn()

    deepseek = fetch_deepseek(old.get('deepseek') or {})
    weather = fetch_weather(old.get('weather') or {})
    news = fetch_news(old.get('news') or '')
    kline = fetch_kline()
    mf = fetch_moneyflow()
    update_strategy(kline, mf)   # 写 state.json strat; message 由 pick_message 读取
    message = pick_message()
    reminders = today_reminders()

    tick_refresh_once()
    tick = build_tick_response()

    data = {
        'deepseek': deepseek,
        'weather': weather,
        'calendar': {'year': now.year, 'month': now.month, 'day': now.day,
                     'hour': now.hour, 'minute': now.minute, 'second': now.second,
                     'weekday': now.isoweekday() % 7},
        'greeting': pick_greeting(),
        'message': message,
        'reminders': reminders,
        'news': news,
    }
    atomic_write(DATA_FILE, json.dumps(data, ensure_ascii=False))
    atomic_write('tick.txt', tick)

    # 退出码: 无法产出任何有效内容才失败 (正常情况 weather 有模拟兜底, 恒为 0)
    has_any = (deepseek.get('balance') is not None or weather is not None
               or news or message or os.path.exists(DATA_FILE))
    print('[sync] %s -> data.json (%.1fKB) tick.txt (%.1fKB)' % (
        now.strftime('%F %T'), os.path.getsize(DATA_FILE) / 1024.0,
        os.path.getsize('tick.txt') / 1024.0 if os.path.exists('tick.txt') else 0))
    return 0 if has_any else 1


if __name__ == '__main__':
    sys.exit(main())
