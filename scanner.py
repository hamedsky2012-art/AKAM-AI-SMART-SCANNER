# AKAM AI SMART SCANNER — OKX V3
# Direct OKX public REST API; no Binance, no CCXT.
import os,time,requests,numpy as np,pandas as pd
from datetime import datetime,timezone
TOKEN=os.environ['TELEGRAM_BOT_TOKEN']; CHAT_ID=os.environ['TELEGRAM_CHAT_ID']
OKX='https://www.okx.com/api/v5'; CG='https://api.coingecko.com/api/v3'
MIN_QUOTE_VOLUME=5_000_000; MAX_LIQUID=500; DEEP_LIMIT=50; MIN_SCORE=85
S=requests.Session(); S.headers.update({'User-Agent':'AKAM-AI-SMART-SCANNER/3.0','Accept':'application/json'})

def get_json(url,params=None,timeout=20,retries=3):
    last=None
    for i in range(retries):
        try:
            r=S.get(url,params=params,timeout=timeout); r.raise_for_status(); d=r.json()
            if isinstance(d,dict) and str(d.get('code','0'))!='0': raise RuntimeError(f"API code={d.get('code')} msg={d.get('msg')}")
            return d
        except Exception as e:
            last=e
            if i<retries-1: time.sleep(1.2*(i+1))
    raise last

def tg(text):
    try:
        r=S.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT_ID,'text':text,'disable_web_page_preview':True},timeout=15); r.raise_for_status()
    except Exception as e: print('Telegram error:',e)

def coingecko_universe(limit=1000):
    coins=[]
    for page in range(1,6):
        d=get_json(f'{CG}/coins/markets',{'vs_currency':'usd','order':'market_cap_desc','per_page':250,'page':page,'sparkline':'false'},25)
        if not d: break
        coins.extend(d)
        if len(d)<250: break
        time.sleep(.3)
    return coins[:limit]

def okx_instruments():
    d=get_json(f'{OKX}/public/instruments',{'instType':'SPOT'})
    return {x['instId']:x for x in d.get('data',[]) if x.get('quoteCcy')=='USDT' and x.get('state')=='live' and x.get('baseCcy')}

def okx_tickers():
    d=get_json(f'{OKX}/market/tickers',{'instType':'SPOT'}); out={}
    for x in d.get('data',[]):
        inst=x.get('instId','')
        if not inst.endswith('-USDT'): continue
        try:
            last=float(x['last']); vol=float(x.get('volCcyQuote') or 0)
            if last>0: out[inst]={'last':last,'quote_volume':vol}
        except Exception: pass
    return out

def build_liquid_universe():
    cg=coingecko_universe(); ins=okx_instruments(); ticks=okx_tickers(); rank={}
    for i,c in enumerate(cg,1):
        sym=str(c.get('symbol','')).upper()
        if sym: rank[sym]={'rank':i,'name':c.get('name',sym),'market_cap':c.get('market_cap') or 0}
    rows=[]
    for inst,t in ticks.items():
        base=inst.split('-')[0]; m=ins.get(inst)
        if not m or t['quote_volume']<MIN_QUOTE_VOLUME: continue
        meta=rank.get(base,{})
        rows.append({'inst':inst,'base':base,'name':meta.get('name',base),'cg_rank':meta.get('rank',99999),'market_cap':meta.get('market_cap',0),'quote_volume':t['quote_volume'],'last':t['last']})
    rows.sort(key=lambda x:(-x['quote_volume'],x['cg_rank']))
    return rows[:MAX_LIQUID]

def candles(inst_id,bar,limit=300):
    d=get_json(f'{OKX}/market/candles',{'instId':inst_id,'bar':bar,'limit':str(min(limit,300))}); parsed=[]
    for r in d.get('data',[]):
        if len(r)<9: continue
        try: parsed.append([int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]),float(r[6]),int(r[8])])
        except Exception: pass
    if not parsed: return pd.DataFrame()
    parsed.reverse(); x=pd.DataFrame(parsed,columns=['ts','open','high','low','close','vol','vol_quote','confirm'])
    return x[x['confirm']==1].reset_index(drop=True)

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0); au=up.ewm(alpha=1/n,adjust=False).mean(); ad=dn.ewm(alpha=1/n,adjust=False).mean(); rs=au/ad.replace(0,np.nan); return (100-100/(1+rs)).fillna(50)
def atr(x,n=14):
    pc=x['close'].shift(1); tr=pd.concat([x['high']-x['low'],(x['high']-pc).abs(),(x['low']-pc).abs()],axis=1).max(axis=1); return tr.ewm(alpha=1/n,adjust=False).mean()
def add(x):
    x=x.copy(); x['ema20']=ema(x['close'],20); x['ema50']=ema(x['close'],50); x['ema200']=ema(x['close'],200); x['rsi']=rsi(x['close']); f=ema(x['close'],12); sl=ema(x['close'],26); x['macd']=f-sl; x['macd_signal']=ema(x['macd'],9); x['hist']=x['macd']-x['macd_signal']; x['atr']=atr(x); x['vol_ma20']=x['vol_quote'].rolling(20).mean(); x['vol_ratio']=x['vol_quote']/x['vol_ma20'].replace(0,np.nan); x['vwap20']=(x['close']*x['vol_quote']).rolling(20).sum()/x['vol_quote'].rolling(20).sum().replace(0,np.nan); x['high20']=x['high'].rolling(20).max().shift(1); x['low20']=x['low'].rolling(20).min().shift(1); return x.dropna().reset_index(drop=True)

def btc_regime():
    try:
        d=add(candles('BTC-USDT','1D',250)); h=add(candles('BTC-USDT','4H',250));
        if d.empty or h.empty:return 5
        a=d.iloc[-1]; b=h.iloc[-1]; score=5
        score += 2 if a['close']>a['ema20'] else 0; score += 1 if a['ema20']>a['ema50'] else 0; score += 1 if a['ema50']>a['ema200'] else 0; score += 1 if b['close']>b['ema20'] else 0
        return max(0,min(10,score))
    except Exception as e: print('BTC regime error:',e); return 5

def setup(x):
    if len(x)<30:return None
    a=x.iloc[-1]; p=x.iloc[-2]
    if a['close']>a['ema50'] and a['ema20']>a['ema50'] and a['low']<=a['ema20']*1.015 and a['close']>a['ema20'] and 45<=a['rsi']<=68:return 'PULLBACK'
    if p['close']>p['high20'] and a['low']<=p['close']*1.01 and a['close']>=p['close']*.995 and a['vol_ratio']>=1.15:return 'BREAKOUT + RETEST'
    if a['low']<=a['low20']*1.015 and a['close']>a['open'] and a['close']>p['close'] and a['rsi']>=38 and a['rsi']>p['rsi']:return 'SUPPORT REVERSAL'
    return None

def confirm(x):
    if len(x)<205:return False
    a=x.iloc[-1]; p=x.iloc[-2]
    return a['close']>a['ema20'] and a['ema20']>=a['ema50'] and a['close']>p['high'] and a['rsi']>=48 and a['hist']>=p['hist']

def signal(row,d,h1,h,btc):
    if min(len(d),len(h1),len(h))<205:return None
    a=d.iloc[-1]; b=h.iloc[-1]; c=h1.iloc[-1]; st=setup(h)
    if not st or not confirm(h1):return None
    score=0
    score += 5 if a['close']>a['ema20'] else 0; score += 5 if a['ema20']>a['ema50'] else 0; score += 5 if a['ema50']>a['ema200'] else 0
    score += 5 if b['close']>b['ema20'] else 0; score += 5 if b['ema20']>b['ema50'] else 0; score += 5 if b['ema50']>b['ema200'] else 0
    score += 4 if b['close']>b['ema20'] else 0; score += 3 if b['ema20']>b['ema50'] else 0; score += 3 if b['ema50']>b['ema200'] else 0
    score += {'PULLBACK':13,'BREAKOUT + RETEST':15,'SUPPORT REVERSAL':12}.get(st,0)
    score += 10 if 50<=b['rsi']<=68 else (6 if 45<=b['rsi']<50 or 68<b['rsi']<=72 else 0)
    score += 6 if b['hist']>0 else 0; score += 4 if b['hist']>h.iloc[-2]['hist'] else 0
    vr=float(b['vol_ratio']); score += 15 if vr>=1.8 else (12 if vr>=1.4 else (8 if vr>=1.15 else 0))
    score += 3 if c['close']>c['ema20'] else 0; score += 3 if c['ema20']>=c['ema50'] else 0; score += 2 if c['rsi']>=50 else 0; score += 2 if c['hist']>=h1.iloc[-2]['hist'] else 0
    score += int(round(btc))
    entry=float(c['close']); at=float(b['atr'])
    if not np.isfinite(entry) or not np.isfinite(at) or at<=0 or entry>float(b['ema20'])*1.10:return None
    stop=min(float(b['low20']),entry-1.2*at); stop=stop if stop<entry else entry-1.2*at; risk=entry-stop
    if risk<=0:return None
    return {'inst':row['inst'],'base':row['base'],'score':int(score),'setup':st,'entry':entry,'sl':stop,'tp1':entry+2.5*risk,'tp2':entry+3*risk,'tp3':entry+4*risk,'volume':row['quote_volume'],'rsi':float(b['rsi']),'vol_ratio':vr}

def fp(v):
    if v>=1000:return f'{v:,.2f}'
    if v>=1:return f'{v:,.4f}'
    if v>=.01:return f'{v:,.6f}'
    return f'{v:.8f}'
def money(v): return f'${v/1e9:.2f}B' if v>=1e9 else (f'${v/1e6:.1f}M' if v>=1e6 else f'${v/1e3:.1f}K')

def message(signals,n,btc,elapsed):
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'); lines=['🤖 AKAM AI SMART SCANNER','━━━━━━━━━━━━━━━━━━','🟢 OKX V3 | Spot USDT',f'🕐 {now}',f'🌐 Liquid universe: {n}',f'🧠 BTC regime: {btc}/10',f'⏱ Scan time: {elapsed:.1f}s','']
    if not signals: lines += ['🔎 نتیجه اسکن','سیگنال واجد شرایط پیدا نشد.','',f'فیلتر: Score ≥ {MIN_SCORE} + تأیید 1H + No-Chase','','𓆩 AKAM FINANCIAL 𓆪','https://t.me/Trade_Akam']; return '\n'.join(lines)
    lines += [f'🔥 {len(signals)} SIGNAL FOUND','']
    for i,s in enumerate(signals,1): lines += [f"#{i} {s['base']} | SCORE {s['score']}/110",f"📌 Setup: {s['setup']}",f"🎯 Entry: {fp(s['entry'])}",f"🛑 SL: {fp(s['sl'])}",f"🥇 TP1: {fp(s['tp1'])}",f"🥈 TP2: {fp(s['tp2'])}",f"🥉 TP3: {fp(s['tp3'])}",f"📊 RSI: {s['rsi']:.1f} | Vol: {s['vol_ratio']:.2f}x",f"💧 24h Vol: {money(s['volume'])}",'']
    lines += ['⚠️ این خروجی اسکن تکنیکال است و تضمین سود نیست.','','𓆩 AKAM FINANCIAL 𓆪','https://t.me/Trade_Akam']; return '\n'.join(lines)

def main():
    start=time.time(); print('AKAM V3 starting — OKX direct REST')
    liquid=build_liquid_universe(); print(f'Universe liquid={len(liquid)}')
    if not liquid: tg('❌ AKAM AI SMART SCANNER\n\nهیچ بازار USDT واجد شرایطی از OKX دریافت نشد.'); return
    btc=btc_regime(); print(f'BTC regime score={btc}/10'); candidates=liquid[:DEEP_LIMIT]; print(f'Deep analysis: {len(candidates)}'); signals=[]
    for i,row in enumerate(candidates,1):
        try:
            d=add(candles(row['inst'],'1D',250)); h=add(candles(row['inst'],'4H',250)); h1=add(candles(row['inst'],'1H',250))
            if min(len(d),len(h),len(h1))>=205:
                z=signal(row,d,h1,h,btc)
                if z and z['score']>=MIN_SCORE: signals.append(z); print(f"SIGNAL {z['base']} score={z['score']} setup={z['setup']}")
            if i%10==0: print(f'Deep {i}/{len(candidates)}')
        except Exception as e: print(f"deep skip {row['inst']}: {e}")
        time.sleep(.08)
    signals=sorted(signals,key=lambda x:(x['score'],x['vol_ratio']),reverse=True)[:5]; elapsed=time.time()-start; print(f'Signals found={len(signals)}'); print(f'Finished in {elapsed:.1f}s'); tg(message(signals,len(liquid),btc,elapsed))

if __name__=='__main__': main()
                 
