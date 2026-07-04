#!/usr/bin/env python3
"""
九天·财神 — 选股扫描引擎 v3.0（HTTP API版）
给客户用的：零依赖，GitHub Actions 上跑，不用 WSL 不用 mootdx
数据源：东方财富 push2 HTTP API

用法：
    python3 scanner_http.py                     # 全量扫描+打印报告
    python3 scanner_http.py --pushplus TOKEN    # 扫描+推送到 PushPlus 微信
    STOCK_LIST=600519,000001 python3 scanner_http.py  # 自定义股票池
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime

# ==================== 配置 ====================
MIN_PRICE = 5
MIN_VOLUME = 500_000  # 最少成交量（手）
EXCLUDE_ST = True
TOP_CANDIDATES = 50
FINAL_TOP = 5

# 多因子权重
W_TECH = 0.35
W_SENT = 0.25
W_MACRO = 0.25
W_FUND = 0.15

# A股有效前缀
VALID_PREFIXES = ('600', '601', '603', '605',
                  '000', '001', '002', '003',
                  '300', '301',
                  '688')

# ==================== 日志 ====================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ==================== HTTP 工具 ====================
def http_get(url, timeout=10):
    """带重试的 HTTP GET"""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://quote.eastmoney.com/',
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode('utf-8')
                if data.startswith('jQuery'):
                    data = data[data.index('(')+1:data.rindex(')')]
                return json.loads(data)
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            raise


# ==================== 数据获取 ====================
def fetch_all_stocks():
    """
    从东方财富获取全市场 A 股行情
    用 fs 参数分市场获取：深市主板+创业板+沪市主板+科创板
    """
    log("获取全市场 A 股行情...")
    
    # 东财 fs 过滤参数
    markets = [
        'm:0+t:6',    # 深市主板
        'm:0+t:80',   # 创业板
        'm:1+t:2',    # 沪市主板
        'm:1+t:23',   # 科创板
    ]
    fs = ','.join(markets)
    
    all_items = []
    page = 1
    
    while True:
        url = (
            f"https://push2.eastmoney.com/api/qt/clist/get"
            f"?cb=&pn={page}&pz=500&po=1&np=1"
            f"&ut=bd1d9ddb04089700cf9c27f6f7426281"
            f"&fltt=2&invt=2&fid=f3&fs={fs}"
            f"&fields=f12,f14,f2,f3,f4,f5,f6,f15,f17,f18,f20,f21"
        )
        
        try:
            result = http_get(url)
        except Exception as e:
            log(f"  请求失败: {e}")
            break
        
        data = result.get('data', {})
        if not data or 'diff' not in data or not data['diff']:
            break
        
        items = data['diff']
        all_items.extend(items)
        
        total = data.get('total', 0)
        log(f"  第{page}页: {len(items)}只 (累计{len(all_items)}/{total})")
        
        if len(all_items) >= total:
            break
        
        page += 1
        time.sleep(0.3)
    
    log(f"  总计获取: {len(all_items)}只")
    return all_items


def parse_stock(item):
    """解析东财返回的单个股票数据"""
    try:
        code = str(item.get('f12', '')).zfill(6)
        name = str(item.get('f14', ''))
        
        # 过滤前缀
        if not any(code.startswith(p) for p in VALID_PREFIXES):
            return None
        
        # 过滤 ST
        if EXCLUDE_ST and ('ST' in name or '*ST' in name):
            return None
        
        price = item.get('f2')  # 最新价
        if price is None or price == '-':
            return None
        price = float(price)
        
        if price < MIN_PRICE:
            return None
        
        volume = item.get('f5', 0)  # 成交量（手）
        if volume == '-':
            return None
        volume = float(volume)
        if volume < MIN_VOLUME:
            return None
        
        change_pct = item.get('f3', 0)  # 涨跌幅
        if change_pct == '-':
            return None
        change_pct = float(change_pct)
        
        # 排除涨停/跌停
        if change_pct >= 9.5:
            return None
        if change_pct <= -7:
            return None
        
        amount = item.get('f6', 0)  # 成交额
        if amount == '-':
            amount = 0
        amount = float(amount)
        
        high = item.get('f15', 0)
        low = item.get('f16', 0)
        open_price = item.get('f17', 0)
        pre_close = item.get('f18', 0)
        
        if high == '-': high = 0
        if low == '-': low = 0
        if open_price == '-': open_price = 0
        if pre_close == '-': pre_close = 0
        
        high = float(high)
        low = float(low) if low else price
        open_price = float(open_price) if open_price else price
        pre_close = float(pre_close) if pre_close and pre_close != '-' else price
        
        return {
            'code': code,
            'name': name,
            'market': 'SH' if code.startswith(('6', '9')) else 'SZ',
            'price': price,
            'pre_close': pre_close,
            'volume': volume,
            'change_pct': change_pct,
            'amount': amount,
            'open': open_price,
            'high': high,
            'low': low,
            'turnover': item.get('f20', 0),  # 换手率
            'pe': item.get('f21', 0),  # 动态市盈率
        }
    except (ValueError, TypeError):
        return None


# ==================== 多因子打分 ====================
def score_stocks(candidates):
    """多因子打分（完全不依赖 AI）"""
    log("多因子打分...")
    
    for c in candidates:
        chg = c['change_pct']
        vol = c['volume']
        amount = c['amount']
        price = c['price']
        code = c['code']
        pe = c.get('pe', 0)
        
        # === 技术面 (35%) ===
        tech = 50
        
        if 2 <= chg <= 5:
            tech += 20
        elif 5 < chg <= 7:
            tech += 10
        elif 0 < chg < 2:
            tech += 8
        elif chg > 7:
            tech -= 15
        elif -2 <= chg < 0:
            tech += 3
        elif chg < -5:
            tech -= 15
        
        if amount > 500_000_000:
            tech += 15
        elif amount > 100_000_000:
            tech += 8
        elif amount > 50_000_000:
            tech += 3
        
        if c['open'] > 0 and price >= c['open']:
            tech += 5
        
        tech = max(0, min(100, tech))
        
        # === 情绪面 (25%) ===
        sent = 50
        
        if vol > 10_000_000:
            sent += 20
        elif vol > 5_000_000:
            sent += 10
        
        if 3 <= chg <= 7:
            sent += 15
        elif 1 <= chg < 3:
            sent += 5
        
        sent = max(0, min(100, sent))
        
        # === 宏观面 (25%) ===
        macro = 50
        if code.startswith('300') or code.startswith('688'):
            macro += 10
        if code.startswith('600') or code.startswith('601'):
            macro += 5
        
        macro = max(0, min(100, macro))
        
        # === 基本面 (15%) ===
        fund = 50
        if 10 <= price <= 50:
            fund += 10
        if amount > 100_000_000:
            fund += 10
        if 0 < pe < 50:
            fund += 5
        
        fund = max(0, min(100, fund))
        
        total = (tech * W_TECH + sent * W_SENT +
                 macro * W_MACRO + fund * W_FUND)
        
        c['scores'] = {
            'technical': tech,
            'sentiment': sent,
            'macro': macro,
            'fundamental': fund,
            'total': round(total, 1)
        }
    
    candidates.sort(key=lambda x: x['scores']['total'], reverse=True)
    return candidates


# ==================== 推送 ====================
def push_to_pushplus(token, title, content):
    """通过 PushPlus 推送到微信"""
    url = "https://www.pushplus.plus/send"
    data = json.dumps({
        "token": token,
        "title": title,
        "content": content,
        "template": "txt",
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json',
    })
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get('code') == 200:
                log("✅ 微信推送成功")
            else:
                log(f"⚠️ 推送失败: {result}")
    except Exception as e:
        log(f"❌ 推送异常: {e}")


# ==================== 报告格式化 ====================
def format_report(candidates):
    """微信消息格式化"""
    today = datetime.now().strftime('%Y年%m月%d日')
    
    report = f"""📊 九天·财神 盘前简报
{'='*30}
📅 {today}

"""
    
    if not candidates:
        report += "⚠️ 今日未筛选出符合条件的标的，建议观望。\n"
        return report
    
    report += f"🔍 全A股多因子扫描 → Top {min(FINAL_TOP, len(candidates))} 推荐\n\n"
    
    for i, c in enumerate(candidates[:FINAL_TOP]):
        name = c['name']
        code = c['code']
        price = c['price']
        chg = c['change_pct']
        amt = c['amount'] / 100_000_000
        s = c['scores']
        
        reasons = []
        if s['technical'] >= 70:
            reasons.append("技术面强势")
        if s['sentiment'] >= 65:
            reasons.append("资金关注度高")
        if chg >= 2:
            reasons.append(f"涨幅{chg:.1f}%")
        if amt >= 1:
            reasons.append(f"成交{amt:.1f}亿")
        
        reason_str = "，".join(reasons[:3]) if reasons else "综合评分领先"
        
        report += f"""{i+1}️⃣ {name}（{code}）
   现价 ¥{price:.2f} | {chg:+.2f}% | 成交{amt:.2f}亿
   技术{s['technical']} | 情绪{s['sentiment']} | 宏观{s['macro']} | 基本面{s['fundamental']}
   📈 总分: {s['total']} — {reason_str}

"""
    
    report += f"""{'='*30}
⚠️ 免责声明：AI自动生成，仅供参考，不构成投资建议。
投资有风险，入市需谨慎。
"""
    return report


# ==================== 主函数 ====================
def main():
    log("═══ 九天·财神 选股扫描 v3.0（HTTP版）═══")
    log(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 环境变量支持自定义股票池
    stock_list_env = os.environ.get('STOCK_LIST', '')
    
    # Step 1: 获取行情
    raw_stocks = fetch_all_stocks()
    
    # Step 2: 解析过滤
    log("解析过滤...")
    candidates = []
    for item in raw_stocks:
        parsed = parse_stock(item)
        if parsed:
            candidates.append(parsed)
    
    log(f"  通过基础筛选: {len(candidates)}只")
    
    # Step 3: 打分排序
    scored = score_stocks(candidates)
    
    # Step 4: 生成报告
    report = format_report(scored)
    print("\n" + report)
    
    # Step 5: 保存本地
    out_dir = os.path.expanduser("~/蜂鸟传媒/财神")
    os.makedirs(out_dir, exist_ok=True)
    fname = os.path.join(out_dir, f"scan_{datetime.now().strftime('%Y%m%d')}.txt")
    with open(fname, 'w', encoding='utf-8') as f:
        f.write(report)
    log(f"✅ 报告已保存: {fname}")
    
    # Step 6: 推送到微信（通过 PushPlus）
    pushplus_token = os.environ.get('PUSHPLUS_TOKEN', '')
    if pushplus_token:
        push_to_pushplus(
            pushplus_token,
            f"九天·财神 盘前简报 {datetime.now().strftime('%m/%d')}",
            report
        )
    
    return report


if __name__ == "__main__":
    # 支持命令行参数 --pushplus TOKEN
    if len(sys.argv) >= 3 and sys.argv[1] == '--pushplus':
        os.environ['PUSHPLUS_TOKEN'] = sys.argv[2]
    main()
